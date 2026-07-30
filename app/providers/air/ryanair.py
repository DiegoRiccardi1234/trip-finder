"""Ryanair, tramite l'API pubblica del Fare Finder.

Due endpoint, entrambi senza autenticazione:

  - `views/locate/.../routes/.../airport/XXX` elenca le rotte servite da uno
    scalo. Si interroga una volta al mese e si tiene in cache: senza, la ricerca
    chiederebbe a Ryanair anche le tratte che non vola, sprecando richieste su
    ogni combinazione di aeroporti.
  - `farfnd/v4/oneWayFares` restituisce orari e prezzo del volo piu' economico
    del giorno.

L'endpoint `booking/v4/availability`, che darebbe tutti i voli e tutte le
tariffe, rifiuta le richieste senza una sessione di prenotazione valida
(risponde 409 "Availability declined"): non e' utilizzabile qui.

Nota sulle tariffe: il prezzo di Ryanair e' la tariffa base, che include solo
una borsa sotto il sedile. Il bagaglio a mano grande e quello in stiva sono
supplementi, e li aggiunge `routing/cost.py` in modo uniforme per tutte le
compagnie.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.models import Fare, Leg, Mode, Node, NodeKind, node_distance_km
from app.providers import coverage
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

FARES_URL = "https://services-api.ryanair.com/farfnd/v4/oneWayFares"
ROUTES_URL = "https://www.ryanair.com/api/views/locate/searchWidget/routes/it/airport"
BOOKING_URL = "https://www.ryanair.com/it/it/trip/flights/select"

#: Emissioni per passeggero-km sul corto raggio, comprensive del fattore di
#: forzante radiativa che rende un volo peggiore della sola combustione.
CO2_KG_PER_KM = 0.18


@register
class Ryanair(Provider):
    id = "ryanair"
    name = "Ryanair"
    mode = Mode.AIR
    tier = 1
    website = "https://www.ryanair.com/"
    sample_route = ("Milano", "Catania")

    def supports_node(self, node: Node) -> bool:
        """Un aeroporto si identifica col codice IATA, non con un ID di dataset.

        Se la rotta esista davvero si scopre in `fetch`, dove possiamo fare la
        richiesta (tenuta in cache) all'elenco delle rotte servite."""
        return node.kind is NodeKind.AIRPORT and bool(node.iata)

    def route_key(self, origin: Node, destination: Node) -> tuple[str, str, str]:
        return (self.id, origin.iata or origin.id, destination.iata or destination.id)

    async def _routes_from(self, iata: str, ctx: SearchContext) -> set[str] | None:
        """Aeroporti raggiungibili da uno scalo. `None` se non lo sappiamo."""

        async def load() -> list[str]:
            return sorted(await self._fetch_routes(iata, ctx))

        # Qui il vuoto e' una risposta vera: se l'elenco rotte di uno scalo non
        # esiste (404), Ryanair da li' non vola, e vale la pena ricordarlo.
        known = await coverage.ensure(self.id, iata, load, allow_empty=True)
        return set(known) if known is not None else None

    async def _fetch_routes(self, iata: str, ctx: SearchContext) -> set[str]:
        try:
            response = await ctx.http.get(
                f"{ROUTES_URL}/{iata}",
                headers={"Accept": "application/json"},
                allow_status={404},
            )
        except HttpError as exc:
            raise ProviderError(f"elenco rotte non disponibile: {exc}") from exc

        if response.status_code == 404:
            # Nessun file di rotte per quello scalo significa che Ryanair non ci
            # vola. E' un'informazione utile e stabile: l'insieme vuoto finisce
            # in cache e la tratta non verra' piu' richiesta.
            return set()

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"elenco rotte non interpretabile: {exc}") from exc

        return {
            entry["arrivalAirport"]["code"]
            for entry in data or []
            if isinstance(entry, dict)
            and isinstance(entry.get("arrivalAirport"), dict)
            and entry["arrivalAirport"].get("code")
        }

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        origin_iata, dest_iata = origin.iata, destination.iata
        if not origin_iata or not dest_iata:
            raise NotServed("servono due aeroporti con codice IATA")

        routes = await self._routes_from(origin_iata, ctx)
        # `None` significa che l'elenco rotte non e' arrivato: in quel caso si
        # prova lo stesso, perche' non sapere non e' un rifiuto.
        if routes is not None and dest_iata not in routes:
            raise NotServed(f"Ryanair non vola {origin_iata}-{dest_iata}")

        params = {
            "departureAirportIataCode": origin_iata,
            "arrivalAirportIataCode": dest_iata,
            "outboundDepartureDateFrom": ctx.date.isoformat(),
            "outboundDepartureDateTo": ctx.date.isoformat(),
            "currency": "EUR",
            "adultPaxCount": str(max(1, ctx.pax)),
        }
        try:
            return await ctx.http.get_json(
                FARES_URL, params=params, headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")

        legs: list[Leg] = []
        for fare in raw.get("fares") or []:
            outbound = fare.get("outbound") if isinstance(fare, dict) else None
            if not isinstance(outbound, dict):
                continue
            leg = self._to_leg(outbound, origin, destination, ctx)
            if leg is not None:
                legs.append(leg)
        return legs

    def _to_leg(
        self, outbound: dict, origin: Node, destination: Node, ctx: SearchContext
    ) -> Leg | None:
        depart = _local(outbound.get("departureDate"), origin)
        arrive = _local(outbound.get("arrivalDate"), destination)
        if depart is None or arrive is None or arrive <= depart:
            return None

        price = (outbound.get("price") or {}).get("value")
        fare = None
        notes = ["tariffa base: solo una borsa piccola sotto il sedile"]
        if isinstance(price, (int, float)):
            fare = Fare(
                amount=float(price),
                currency=(outbound.get("price") or {}).get("currencyCode", "EUR"),
                fare_class="Value",
                refundable=False,
                changeable=False,
                included_cabin_bags=0,
                included_checked_bags=0,
            )
        else:
            notes.append("prezzo non disponibile")

        distance_km = node_distance_km(origin, destination)
        return Leg(
            provider=self.id,
            mode=Mode.AIR,
            origin=origin,
            destination=destination,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=outbound.get("flightNumber"),
            fare=fare,
            booking_url=_booking_url(origin, destination, ctx),
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            notes=notes,
        )


def _local(value: Any, node: Node) -> datetime | None:
    """Ryanair pubblica orari locali senza fuso: glielo attacchiamo noi.

    Senza fuso un volo Milano-Londra risulterebbe di un'ora piu' corto di
    quello che e', e il confronto con il treno sarebbe falsato."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    tz = ZoneInfo(node.timezone) if node.timezone else ZoneInfo("Europe/Rome")
    return parsed.replace(tzinfo=tz)


def _booking_url(origin: Node, destination: Node, ctx: SearchContext) -> str:
    params = {
        "adults": max(1, ctx.pax),
        "dateOut": ctx.date.isoformat(),
        "originIata": origin.iata or "",
        "destinationIata": destination.iata or "",
        "isReturn": "false",
        "isConnectedFlight": "false",
        "discount": "0",
    }
    return f"{BOOKING_URL}?{urlencode(params)}"
