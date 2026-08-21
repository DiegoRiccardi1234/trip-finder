"""FlixBus, tramite l'API pubblica che serve shop.flixbus.com.

Risponde senza chiave e copre quasi tutta l'Europa: e' l'adapter con il miglior
rapporto fra copertura e fragilita'.

FlixBus ragiona per **citta'**, non per fermata: la ricerca Torino-Matera
restituisce tutte le combinazioni fra tutti i capolinea delle due citta'. Per
questo l'adapter e' dichiarato `granularity = "city"`, cosi' il motore non lo
interroga una volta per ogni stazione della stessa citta' ottenendo le stesse
corse. Le fermate reali arrivano nella risposta, ma **senza coordinate**: si
tenta di recuperarle dal dataset Trainline e, quando non si riesce, si tengono
quelle della localita' richiesta. Vedi `_station_node`: e' una approssimazione
dichiarata, non un dettaglio.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import orjson

from app.models import Fare, Leg, Mode, Node, NodeKind, haversine_km
from app.orchestrator import cache
from app.providers.base import NotServed, Provider, ProviderError, SearchContext, city_key
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "https://global.api.flixbus.com/search/autocomplete/cities"
SEARCH_URL = "https://global.api.flixbus.com/search/service/v4/search"
SHOP_URL = "https://shop.flixbus.it/search"

CO2_KG_PER_KM = 0.029  # pullman a lunga percorrenza, per passeggero-km

#: Quanto puo' distare la citta' che FlixBus propone da quella che abbiamo
#: chiesto. Misurato il 2026-08-22 sull'autocomplete: quando indovina sta a
#: 0,0-3,1 km (Milano 0,4 · Parigi 3,1 · Monaco di Baviera 0,0 · Napoli 0,0);
#: quando sbaglia sta a **320 km** (asti -> Castiglione della Pescaia) o a
#: **922 km** (acqui terme -> Lamezia Terme). Fra i due gruppi non c'e' zona
#: grigia, e cinquanta chilometri lasciano passare il caso vero che li avvicina
#: di piu': un aeroporto venduto col nome della citta' grande, come Orio al
#: Serio a 43 km da Milano.
MAX_CITTA_KM = 50.0


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
        if city_key(origin) == city_key(destination):
            return False
        # `super()` e non una copia delle sue condizioni: i filtri comuni
        # crescono nella base, e un adapter che se li riscrive smette di
        # riceverli senza che nessuno se ne accorga.
        return super().can_serve(origin, destination)

    # ------------------------------------------------------------ risoluzione

    async def _city_id(self, node: Node, ctx: SearchContext) -> tuple[str, str]:
        """Nome della fermata -> (uuid citta' FlixBus, nome ufficiale)."""
        query = city_key(node)
        # Il nome della chiave e' cambiato di proposito il 2026-08-22: le voci
        # `flix:city` salvate prima possono contenere traduzioni sbagliate
        # (`asti` -> Castiglione della Pescaia), e restavano valide trenta
        # giorni. Cambiare namespace le lascia scadere da sole invece di
        # chiedere a chi aggiorna di svuotare la cache a mano.
        key = cache.make_key("flix:citta", query, node.country or "")
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
        # in Nigeria. Se conosciamo il paese lo imponiamo — e lo imponiamo per
        # davvero: prima un `same_country or candidates` riapriva i candidati
        # esteri proprio quando il filtro aveva trovato qualcosa da togliere,
        # cioe' un filtro che si spegneva da solo nel momento in cui serviva.
        candidates = [entry for entry in data if isinstance(entry, dict)]
        if node.country:
            candidates = [
                entry
                for entry in candidates
                if (entry.get("country") or "").upper() == node.country.upper()
            ]

        # E l'autocomplete e' **fuzzy**: risponde sempre qualcosa. Misurato il
        # 2026-08-22: `q=asti` propone "Castiglione della Pescaia" con
        # punteggio 17,95, `q=acqui terme` propone "Lamezia Terme" con 22,17.
        # Prendere il punteggio piu' alto senza guardare il nome e' il difetto
        # che ha portato una corsa Toscana-Calabria dentro una ricerca fra due
        # paesi dell'astigiano. Il punteggio ordina i candidati buoni; non
        # decide quali lo siano.
        # Il criterio e' **geografico**, non testuale, perche' i nomi non
        # reggono: i due cataloghi usano esonimi diversi ("Parigi"/"Paris",
        # "Monaco di Baviera"/"Munchen") e un confronto sui nomi o li rifiuta o
        # si allarga tanto da riaccettare Castiglione. Le coordinate non hanno
        # questo problema: verificato che l'autocomplete le porta su tutti i
        # candidati di tutte le query provate. Il confronto sui nomi resta come
        # ripiego per il giorno in cui cambiassero forma.
        buoni = [entry for entry in candidates if _e_qui(entry, node, query)]
        if not buoni:
            raise NotServed(f"FlixBus non conosce {query!r}")

        best = max(buoni, key=lambda entry: entry.get("score") or 0.0)
        city_id, name = best["id"], best.get("name") or query

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


def _e_qui(entry: dict, node: Node, query: str) -> bool:
    """Se il candidato dell'autocomplete e' davvero il posto che abbiamo chiesto.

    Prima le coordinate, che sono un fatto; il nome solo se le coordinate non
    ci sono, perche' il nome e' un'opinione di due cataloghi diversi."""
    if not entry.get("id"):
        return False

    posizione = entry.get("location")
    if isinstance(posizione, dict):
        lat, lon = posizione.get("lat"), posizione.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return haversine_km(node.lat, node.lon, float(lat), float(lon)) <= MAX_CITTA_KM

    return _stesso_posto(query, entry.get("name") or "")


def _stesso_posto(query: str, trovato: str) -> bool:
    """Se il nome che FlixBus propone e' davvero il posto che abbiamo chiesto.

    Il confronto e' **per parole intere**, e non per sottostringa, perche' qui
    la sottostringa mente: `castiglione della pescaia` contiene `asti` dentro
    `castiglione`, ed e' esattamente il modo in cui una ricerca Asti-Canelli e'
    finita in Toscana.

    Vale in tutte e due le direzioni perche' i due cataloghi si troncano a
    vicenda: noi possiamo chiedere "San Marzano Oliveto" dove loro scrivono
    "San Marzano", e loro possono scrivere "Bolzano/Bozen" dove noi diciamo
    "Bolzano". Chiedere l'uguaglianza esatta perderebbe fermate vere."""
    from app.geo.datasets import normalize

    chieste = set(normalize(query).split())
    trovate = set(normalize(trovato).split())
    if not chieste or not trovate:
        return False
    return chieste <= trovate or trovate <= chieste


def _shop_url(result: dict, depart: datetime, pax: int) -> str:
    params = {
        "departureCity": (result.get("departure") or {}).get("city_id", ""),
        "arrivalCity": (result.get("arrival") or {}).get("city_id", ""),
        "rideDate": depart.strftime("%d.%m.%Y"),
        "adult": max(1, pax),
    }
    return f"{SHOP_URL}?{urlencode(params)}"


def _station_node(station_id: Any, stations: dict, fallback: Node) -> Node:
    """Nodo della fermata realmente servita, con il nome vero e le coordinate
    migliori che abbiamo.

    La risposta di FlixBus da' il nome della fermata ma non le sue coordinate.
    Le recuperiamo dal dataset Trainline attraverso il `legacy_id`, che e' lo
    stesso identificatore che Trainline pubblica nella colonna `flixbus_id`.
    Quando la fermata non e' nel dataset si tiene la posizione della localita'
    richiesta.

    **Quanto spesso succede: sempre.** Misurato il 2026-08-22 sulle due fixture
    salvate (Milano-Roma e Torino-Matera, 55 gambe): l'indice non ha agganciato
    una sola fermata, perche' la colonna `flixbus_id` copre 1174 nodi su
    cinquantacinquemila. Il ripiego non e' il caso raro, e' il caso normale.

    Finche' la coppia richiesta e' quella giusta l'errore resta dentro la
    citta' ed e' innocuo. Quando non lo e', questo nodo diventa un ibrido — il
    nome di un posto con le coordinate di un altro — e nessun controllo basato
    sulle coordinate puo' vederlo. E' il motivo per cui `Provider.search`
    controlla anche la **durata**, che e' l'unica cosa che questo ripiego non
    puo' falsificare."""
    info = stations.get(station_id) if isinstance(stations, dict) else None
    if not isinstance(info, dict):
        return fallback

    # Se un giorno la risposta portasse le coordinate, sono queste a vincere:
    # sono il dato di prima mano e non hanno bisogno di essere corroborate.
    posizione = info.get("location")
    if isinstance(posizione, dict) and isinstance(posizione.get("lat"), (int, float)):
        return Node(
            id=f"flix:{station_id}",
            name=str(info.get("name") or fallback.name),
            kind=NodeKind.BUS_STOP,
            lat=float(posizione["lat"]),
            lon=float(posizione["lon"]),
            country=fallback.country,
            city=fallback.city,
            timezone=fallback.timezone,
            provider_ids={"flixbus": str(info.get("legacy_id") or station_id)},
        )

    legacy_id = info.get("legacy_id")
    if legacy_id is not None:
        from app.geo.datasets import load_index

        known = load_index().lookup("flixbus", str(legacy_id))
        # **Corroborato, non creduto.** La colonna `flixbus_id` di Trainline non
        # e' il catalogo delle autostazioni FlixBus: misurato il 2026-08-22,
        # delle 27 fermate nelle fixture ne aggancia due, e sono la stessa —
        # «Genova (Fanti d'Italia/Principe)», che l'indice risolve in
        # **Antibes**, in Francia, a 170 km da Torino. L'unico aggancio che
        # avevamo era sbagliato, e questo ramo lo dava per buono senza
        # guardarlo. Una fermata di questa tratta sta vicino alla localita'
        # richiesta: se non ci sta, non e' lei.
        if known is not None:
            distanza = haversine_km(fallback.lat, fallback.lon, known.lat, known.lon)
            if distanza <= MAX_CITTA_KM:
                return known
            logger.debug(
                "flixbus: l'indice dice %s per %s, ma sono %.0f km da %s: non ci credo",
                known.name,
                info.get("name"),
                distanza,
                fallback.name,
            )

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
