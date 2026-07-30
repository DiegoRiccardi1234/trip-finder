"""SBB/CFF, le ferrovie svizzere, tramite l'API pubblica transport.opendata.ch.

L'API ufficiale (`journey-service.api.sbb.ch`) vuole una chiave del portale
SBB. Questa no: nessuna autenticazione, CORS aperto, e i dati arrivano dallo
stesso orario svizzero. E' la via che il progetto puo' usare davvero.

  GET transport.opendata.ch/v1/connections?from=…&to=…&date=…&time=…
  GET transport.opendata.ch/v1/locations?query=…

Gli identificatori sono i codici UIC, che il dataset Trainline porta gia' nella
colonna `cff_id`: Zurigo HB e' `8503000`, Milano Centrale `8301700`. Quindi non
serve risolvere i nomi, che sarebbe anche rischioso (cercando "Vienna" questo
servizio propone alberghi svizzeri).

Due avvertenze, verificate sulle risposte reali:

  - **gli errori sono silenziosi**: con parametri mancanti risponde 200 e una
    lista vuota, non un codice di errore. Una lista vuota va quindi verificata,
    non presa per buona;
  - **non ci sono prezzi.** Le gambe escono senza tariffa e `routing/cost.py`
    la stima dichiarandola. La Svizzera e' cara e la stima lo sottovaluta: la
    nota sulla gamba lo dice esplicitamente.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from app.models import Leg, Mode, Node, NodeKind, node_distance_km
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

CONNECTIONS_URL = "https://transport.opendata.ch/v1/connections"
BOOKING_URL = "https://www.sbb.ch/en/buying/pages/fahrplan/fahrplan.xhtml"

#: Ferrovia svizzera: rete elettrificata, energia in gran parte idroelettrica.
CO2_KG_PER_KM = 0.008

RESULTS = 6

#: La Svizzera e i paesi che i suoi diretti raggiungono.
COUNTRIES = frozenset({"CH", "IT", "DE", "FR", "AT", "LI"})


@register
class SBB(Provider):
    id = "sbb"
    name = "SBB CFF FFS"
    mode = Mode.RAIL
    tier = 1
    countries = COUNTRIES
    has_prices = False
    website = BOOKING_URL
    #: Nomi locali, come nei dataset. Gli alias italiani stanno negli override.
    sample_route = ("Zurich", "Bern")

    def supports_node(self, node: Node) -> bool:
        return node.kind is NodeKind.STATION and bool(self.native_id(node))

    def can_serve(self, origin: Node, destination: Node) -> bool:
        """Almeno un capo in Svizzera.

        Come per OBB: il dataset attribuisce un `cff_id` anche a Milano e
        Monaco, perche' SBB le vende, ma un Milano-Roma non e' affar suo."""
        if not super().can_serve(origin, destination):
            return False
        return "CH" in {origin.country, destination.country}

    # ---------------------------------------------------------------- ricerca

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        origin_id, dest_id = self.native_id(origin), self.native_id(destination)
        if not origin_id or not dest_id:
            raise NotServed("stazione senza identificatore UIC per SBB")

        params = {
            "from": origin_id,
            "to": dest_id,
            "date": ctx.date.isoformat(),
            "time": "00:00",
            "limit": RESULTS,
        }
        try:
            payload = await ctx.http.get_json(
                CONNECTIONS_URL, params=params, headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

        if not isinstance(payload, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")
        # Il servizio non usa i codici HTTP per gli errori: se non ha nemmeno
        # riconosciuto le due stazioni, lo dice mettendo a nulla `from` e `to`.
        if payload.get("from") is None or payload.get("to") is None:
            raise ProviderError(
                f"stazioni non riconosciute: {origin_id} -> {dest_id}"
            )
        return payload

    # ---------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")
        connections = raw.get("connections")
        if connections is None:
            raise ProviderError("risposta senza il blocco delle soluzioni")

        legs: list[Leg] = []
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            leg = self._to_leg(connection, origin, destination, ctx)
            if leg is not None:
                legs.append(leg)
        legs.sort(key=lambda item: item.depart)
        return legs

    def _to_leg(
        self, connection: dict, origin: Node, destination: Node, ctx: SearchContext
    ) -> Leg | None:
        depart = _moment((connection.get("from") or {}).get("departure"))
        arrive = _moment((connection.get("to") or {}).get("arrival"))
        if depart is None or arrive is None or arrive <= depart:
            return None
        if depart.date() != ctx.date:
            return None

        products = [str(p).strip() for p in connection.get("products") or [] if p]
        changes = int(connection.get("transfers") or 0)

        segments: list[str] = []
        for section in connection.get("sections") or []:
            journey = (section or {}).get("journey")
            if not isinstance(journey, dict):
                continue
            label = f"{journey.get('category', '')}{journey.get('number', '')}".strip()
            towards = journey.get("to")
            if label or towards:
                segments.append(f"{label} -> {towards}".strip())

        notes = [
            "orario svizzero senza tariffa: il prezzo si vede sul sito SBB, "
            "ed e' tipicamente piu' alto della stima"
        ]
        if changes:
            notes.append(f"{changes} cambi")

        platform = (connection.get("from") or {}).get("platform")
        if platform:
            notes.append(f"binario di partenza {platform}")

        distance_km = node_distance_km(origin, destination)
        return Leg(
            provider=self.id,
            mode=Mode.RAIL,
            origin=origin,
            destination=destination,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=" + ".join(products[:3]) or None,
            fare=None,
            booking_url=_booking_url(origin, destination, ctx),
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            internal_changes=changes,
            segments=segments[:6],
            notes=notes,
        )


def _moment(value: Any) -> datetime | None:
    """Gli orari arrivano in ISO 8601 con l'offset attaccato, `+0200`."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _booking_url(origin: Node, destination: Node, ctx: SearchContext) -> str:
    params = {
        "von": origin.name,
        "nach": destination.name,
        "datum": ctx.date.strftime("%d.%m.%Y"),
    }
    return f"{BOOKING_URL}?{urlencode(params)}"
