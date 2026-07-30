"""FlixBus, tramite l'API pubblica che serve shop.flixbus.com.

Risponde senza chiave e copre quasi tutta l'Europa: e' l'adapter con il miglior
rapporto fra copertura e fragilita'.

FlixBus ragiona per **citta'**, non per fermata: la ricerca Torino-Matera
restituisce tutte le combinazioni fra tutti i capolinea delle due citta'. Per
questo l'adapter e' dichiarato `granularity = "city"`, cosi' il motore non lo
interroga una volta per ogni stazione della stessa citta' ottenendo le stesse
corse. Le fermate reali arrivano nella risposta e da li' si costruiscono i nodi
con le coordinate giuste, che servono al calcolo dell'ultimo miglio.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import orjson

from app.models import Fare, Leg, Mode, Node, NodeKind
from app.orchestrator import cache
from app.providers.base import NotServed, Provider, ProviderError, SearchContext, city_key
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "https://global.api.flixbus.com/search/autocomplete/cities"
SEARCH_URL = "https://global.api.flixbus.com/search/service/v4/search"
SHOP_URL = "https://shop.flixbus.it/search"

CO2_KG_PER_KM = 0.029  # pullman a lunga percorrenza, per passeggero-km


@register
class FlixBus(Provider):
    id = "flixbus"
    name = "FlixBus"
    mode = Mode.BUS
    tier = 1
    granularity = "city"
    website = "https://www.flixbus.it/"
    sample_route = ("Milano", "Roma")

    def supports_node(self, node: Node) -> bool:
        # FlixBus ragiona per citta': basta saperne il nome, non serve un ID.
        # Aeroporti e porti restano fuori: non sono capolinea di pullman.
        return node.kind in {NodeKind.BUS_STOP, NodeKind.STATION, NodeKind.CITY}

    def can_serve(self, origin: Node, destination: Node) -> bool:
        if origin.id == destination.id:
            return False
        if city_key(origin) == city_key(destination):
            return False
        return self.supports_node(origin) and self.supports_node(destination)

    # ------------------------------------------------------------ risoluzione

    async def _city_id(self, node: Node, ctx: SearchContext) -> tuple[str, str]:
        """Nome della fermata -> (uuid citta' FlixBus, nome ufficiale)."""
        query = city_key(node)
        key = cache.make_key("flix:city", query, node.country or "")
        cached = await cache.get(key)
        if cached:
            return cached["id"], cached["name"]

        params = {"q": query, "lang": "it"}
        if node.country:
            params["country"] = node.country.lower()
        try:
            data = await ctx.http.get_json(
                AUTOCOMPLETE_URL, params=params, headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(f"autocomplete fallito: {exc}") from exc

        if not isinstance(data, list) or not data:
            raise NotServed(f"FlixBus non conosce {query!r}")

        # L'autocomplete e' globale: senza filtro sul paese "Bari" puo' finire
        # in Nigeria. Se conosciamo il paese lo imponiamo.
        candidates = [entry for entry in data if isinstance(entry, dict)]
        if node.country:
            same_country = [
                entry
                for entry in candidates
                if (entry.get("country") or "").upper() == node.country.upper()
            ]
            candidates = same_country or candidates

        best = max(candidates, key=lambda entry: entry.get("score") or 0.0)
        city_id, name = best.get("id"), best.get("name") or query
        if not city_id:
            raise NotServed(f"FlixBus non conosce {query!r}")

        await cache.set(key, {"id": city_id, "name": name}, kind="resolve")
        return city_id, name

    # ----------------------------------------------------------------- search

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        origin_id, _ = await self._city_id(origin, ctx)
        dest_id, _ = await self._city_id(destination, ctx)
        if origin_id == dest_id:
            raise NotServed("origine e destinazione sono la stessa citta' FlixBus")

        params = {
            "from_city_id": origin_id,
            "to_city_id": dest_id,
            "departure_date": ctx.date.strftime("%d.%m.%Y"),
            "products": orjson.dumps({"adult": max(1, ctx.pax)}).decode(),
            "currency": "EUR",
            "locale": "it",
            "search_by": "cities",
            "include_after_midnight_rides": "1",
        }
        try:
            return await ctx.http.get_json(
                SEARCH_URL, params=params, headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

    # --------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")

        pax = ctx.pax
        on_date = ctx.date
        stations = raw.get("stations") or {}
        operators = raw.get("operators") or {}
        legs: list[Leg] = []

        for trip in raw.get("trips") or []:
            if not isinstance(trip, dict):
                continue
            results = trip.get("results") or {}
            entries = results.values() if isinstance(results, dict) else results
            for result in entries:
                if not isinstance(result, dict):
                    continue
                leg = self._to_leg(result, stations, operators, origin, destination, pax)
                if leg is None:
                    continue
                # `include_after_midnight_rides` fa entrare anche corse del giorno
                # successivo: sono utili al motore ma non se l'utente ha chiesto
                # di partire il 14 e gli si propone il 15.
                if on_date is not None and leg.depart.date() != on_date:
                    continue
                legs.append(leg)
        return legs

    def _to_leg(
        self,
        result: dict,
        stations: dict,
        operators: dict,
        origin: Node,
        destination: Node,
        pax: int,
    ) -> Leg | None:
        if result.get("status") not in (None, "available"):
            return None

        depart = _parse_dt((result.get("departure") or {}).get("date"))
        arrive = _parse_dt((result.get("arrival") or {}).get("date"))
        if depart is None or arrive is None or arrive <= depart:
            return None

        from_node = _station_node(
            (result.get("departure") or {}).get("station_id"), stations, origin
        )
        to_node = _station_node(
            (result.get("arrival") or {}).get("station_id"), stations, destination
        )

        price = result.get("price") or {}
        # `total` e' il prezzo per l'intera prenotazione; noi ragioniamo per
        # persona, come tutti gli altri adapter.
        total = price.get("total_with_platform_fee") or price.get("total")
        fare: Fare | None = None
        notes: list[str] = []
        if isinstance(total, (int, float)):
            fare = Fare(
                amount=round(float(total) / max(1, pax), 2),
                currency="EUR",
                refundable=False,
                included_cabin_bags=1,
                included_checked_bags=1,
                seats_left=(result.get("available") or {}).get("seats"),
            )
        else:
            notes.append("prezzo non disponibile")

        sub_legs = [leg for leg in result.get("legs") or [] if isinstance(leg, dict)]
        operator_names = {
            operators[leg["operator_id"]].get("label")
            for leg in sub_legs
            if isinstance(operators.get(leg.get("operator_id")), dict)
        } - {None}

        segments = [
            f"{_station_name(leg.get('departure', {}).get('station_id'), stations)}"
            f" {_hhmm(leg.get('departure', {}).get('date'))}"
            f" -> {_station_name(leg.get('arrival', {}).get('station_id'), stations)}"
            f" {_hhmm(leg.get('arrival', {}).get('date'))}"
            for leg in sub_legs
        ]

        from app.models import node_distance_km

        distance_km = node_distance_km(from_node, to_node) * 1.35  # strade, non linea d'aria

        return Leg(
            provider=self.id,
            mode=Mode.BUS,
            origin=from_node,
            destination=to_node,
            depart=depart,
            arrive=arrive,
            operator=", ".join(sorted(operator_names)) or self.name,
            vehicle=None,
            fare=fare,
            booking_url=_shop_url(result, depart, pax),
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            internal_changes=max(0, len(sub_legs) - 1),
            segments=segments,
            notes=notes,
        )


def _shop_url(result: dict, depart: datetime, pax: int) -> str:
    params = {
        "departureCity": (result.get("departure") or {}).get("city_id", ""),
        "arrivalCity": (result.get("arrival") or {}).get("city_id", ""),
        "rideDate": depart.strftime("%d.%m.%Y"),
        "adult": max(1, pax),
    }
    return f"{SHOP_URL}?{urlencode(params)}"


def _station_node(station_id: Any, stations: dict, fallback: Node) -> Node:
    """Nodo della fermata realmente servita, non di quella richiesta.

    La risposta di FlixBus da' il nome della fermata ma non le sue coordinate.
    Le recuperiamo dal dataset Trainline attraverso il `legacy_id`, che e' lo
    stesso identificatore che Trainline pubblica nella colonna `flixbus_id`.
    Quando la fermata non e' nel dataset si tiene la posizione della localita'
    richiesta: l'errore resta dentro la citta' e non falsa il confronto."""
    info = stations.get(station_id) if isinstance(stations, dict) else None
    if not isinstance(info, dict):
        return fallback

    legacy_id = info.get("legacy_id")
    if legacy_id is not None:
        from app.geo.datasets import load_index

        known = load_index().lookup("flixbus", str(legacy_id))
        if known is not None:
            return known

    name = info.get("name")
    if not name:
        return fallback
    return Node(
        id=f"flix:{station_id}",
        name=str(name),
        kind=NodeKind.BUS_STOP,
        lat=fallback.lat,
        lon=fallback.lon,
        country=fallback.country,
        city=fallback.city,
        timezone=fallback.timezone,
        provider_ids={"flixbus": str(legacy_id or station_id)},
    )


def _station_name(station_id: Any, stations: dict) -> str:
    info = stations.get(station_id) if isinstance(stations, dict) else None
    if isinstance(info, dict) and info.get("name"):
        return str(info["name"])
    return "?"


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _hhmm(value: Any) -> str:
    parsed = _parse_dt(value)
    return parsed.strftime("%H:%M") if parsed else "?"
