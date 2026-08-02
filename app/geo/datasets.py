"""Caricamento dei dataset geografici offline e costruzione dell'indice dei nodi.

Il problema che questo modulo risolve: ogni operatore usa identificatori propri
per le stesse fermate. Mappare a mano Torino Porta Nuova su Trenitalia, FlixBus,
SNCF e DB e' ingestibile su scala europea. Il dataset Trainline pubblica gia' gli
ID incrociati, quindi qui li carichiamo una volta e li attacchiamo al nodo.

I file grezzi stanno in data/ e si scaricano con scripts/fetch_datasets.py.
Gli aggiunti a mano (porti, ferrovie locali, capolinea bus minori) stanno in
app/geo/overrides.json, versionato nel repo.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config import DATA_DIR
from app.models import Node, NodeKind, haversine_km

logger = logging.getLogger(__name__)

STATIONS_CSV = DATA_DIR / "stations.csv"
AIRPORTS_CSV = DATA_DIR / "airports.csv"
WORLD_CITIES_TXT = DATA_DIR / "cities15000.txt"
OVERRIDES_JSON = Path(__file__).with_name("overrides.json")

#: colonna ID -> colonna booleana che dice se l'operatore serve davvero la fermata.
TRAINLINE_PROVIDER_COLUMNS: dict[str, tuple[str, str | None]] = {
    "trenitalia": ("trenitalia_id", "trenitalia_is_enabled"),
    "italo": ("ntv_id", "ntv_is_enabled"),
    "sncf": ("sncf_id", "sncf_is_enabled"),
    "db": ("db_id", "db_is_enabled"),
    "renfe": ("renfe_id", "renfe_is_enabled"),
    "oebb": ("obb_id", "obb_is_enabled"),
    "sbb": ("cff_id", "cff_is_enabled"),
    "atoc": ("atoc_id", "atoc_is_enabled"),
    "benerail": ("benerail_id", "benerail_is_enabled"),
    "westbahn": ("westbahn_id", "westbahn_is_enabled"),
    "leoexpress": ("leoexpress_id", "leoexpress_is_enabled"),
    "entur": ("entur_id", "entur_is_enabled"),
    "ouigo": ("ouigo_id", "ouigo_is_enabled"),
    "flixbus": ("flixbus_id", "flixbus_is_enabled"),
    "busbud": ("busbud_id", "busbud_is_enabled"),
    "distribusion": ("distribusion_id", "distribusion_is_enabled"),
    "trenord": ("trenord_id", None),
}

RAIL_PROVIDERS = frozenset(
    {
        "trenitalia",
        "italo",
        "sncf",
        "db",
        "renfe",
        "oebb",
        "sbb",
        "atoc",
        "benerail",
        "westbahn",
        "leoexpress",
        "entur",
        "ouigo",
        "trenord",
    }
)
BUS_PROVIDERS = frozenset({"flixbus", "busbud", "distribusion"})

#: Fuso orario di default per paese. La stragrande maggioranza dei paesi europei
#: ne ha uno solo; per gli altri il valore serve solo da fallback quando il
#: dataset non porta il fuso (tipico degli aeroporti di OurAirports).
COUNTRY_TZ: dict[str, str] = {
    "IT": "Europe/Rome",
    "FR": "Europe/Paris",
    "DE": "Europe/Berlin",
    "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon",
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels",
    "LU": "Europe/Luxembourg",
    "CH": "Europe/Zurich",
    "AT": "Europe/Vienna",
    "CZ": "Europe/Prague",
    "SK": "Europe/Bratislava",
    "PL": "Europe/Warsaw",
    "HU": "Europe/Budapest",
    "SI": "Europe/Ljubljana",
    "HR": "Europe/Zagreb",
    "BA": "Europe/Sarajevo",
    "RS": "Europe/Belgrade",
    "ME": "Europe/Podgorica",
    "MK": "Europe/Skopje",
    "AL": "Europe/Tirane",
    "GR": "Europe/Athens",
    "BG": "Europe/Sofia",
    "RO": "Europe/Bucharest",
    "MD": "Europe/Chisinau",
    "UA": "Europe/Kyiv",
    "DK": "Europe/Copenhagen",
    "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo",
    "FI": "Europe/Helsinki",
    "EE": "Europe/Tallinn",
    "LV": "Europe/Riga",
    "LT": "Europe/Vilnius",
    "IS": "Atlantic/Reykjavik",
    "MT": "Europe/Malta",
    "CY": "Asia/Nicosia",
    "TR": "Europe/Istanbul",
    "MA": "Africa/Casablanca",
    "TN": "Africa/Tunis",
}

#: Tipi OurAirports che ci interessano: gli heliport e i campi chiusi no.
AIRPORT_TYPES = frozenset({"large_airport", "medium_airport"})

#: Colonne del dump GeoNames `cities15000.txt`, che non ha intestazione.
GEONAMES_NAME = 1
GEONAMES_ALT_NAMES = 3
GEONAMES_LAT = 4
GEONAMES_LON = 5
GEONAMES_COUNTRY = 8
GEONAMES_POPULATION = 14
GEONAMES_TIMEZONE = 17

#: Nomi alternativi per citta': si tengono tutti quelli scrivibili con una
#: tastiera latina e si buttano ideogrammi, cirillico, greco e arabo.
#:
#: Tagliarne un numero fisso non funziona, ed e' stato provato: «Parigi» e' il
#: ventiduesimo nome di Paris, «Londra» il venticinquesimo, «New York» il
#: ventiquattresimo di New York City. Con un tetto a dodici si perdevano tutti e
#: tre. Tenerli tutti sono 350.000 voci, che in una mappa non pesano — l'unico
#: posto dove un candidato in piu' fa danno e' il matching per somiglianza, e
#: li' gli alias non entrano affatto (vedi `Resolver._corpus`).
MAX_ALIASES = 60

#: Lato della cella per la griglia dei fusi orari, in gradi. Con un grado le
#: celle sono ~110 km: abbastanza fitte da trovare sempre una citta' vicina a un
#: aeroporto, abbastanza larghe da non moltiplicare le celle vuote.
TZ_GRID_STEP = 1.0


def normalize(text: str) -> str:
    """Minuscolo, senza accenti, senza punteggiatura, spazi collassati."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return " ".join(cleaned.split())


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"t", "true", "1", "yes", "y"}


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _to_float(value: str | None) -> float | None:
    try:
        return float((value or "").strip())
    except (TypeError, ValueError):
        return None


@dataclass
class CityEntry:
    """Voce del gazetteer: serve a trasformare "Torino" in un punto sulla mappa."""

    name: str
    normalized: str
    lat: float
    lon: float
    country: str | None = None
    #: Peso usato per rompere i pareggi fra omonimi (es. Roma IT vs Roma altro).
    weight: float = 1.0
    aliases: list[str] = field(default_factory=list)


@dataclass
class GeoIndex:
    nodes: list[Node]
    cities: list[CityEntry]
    by_id: dict[str, Node]
    by_iata: dict[str, Node]
    #: provider -> identificatore nativo -> nodo. Serve agli adapter che nelle
    #: risposte citano una fermata con il proprio ID e non ne danno le
    #: coordinate (FlixBus lo fa): senza posizione non si calcola l'ultimo miglio.
    by_provider: dict[str, dict[str, Node]] = field(default_factory=dict)

    def lookup(self, provider: str, native_id: str) -> Node | None:
        return self.by_provider.get(provider, {}).get(str(native_id))

    def nodes_within(
        self, lat: float, lon: float, radius_km: float, kinds: set[NodeKind] | None = None
    ) -> list[Node]:
        """Nodi entro un raggio, ordinati per distanza crescente."""
        # Pre-filtro rettangolare: evita di calcolare haversine su 55k nodi.
        dlat = radius_km / 111.0
        dlon = radius_km / max(1.0, 111.0 * abs(_cos_deg(lat)))
        found: list[tuple[float, Node]] = []
        for node in self.nodes:
            if abs(node.lat - lat) > dlat or abs(node.lon - lon) > dlon:
                continue
            if kinds is not None and node.kind not in kinds:
                continue
            distance = haversine_km(lat, lon, node.lat, node.lon)
            if distance <= radius_km:
                found.append((distance, node))
        found.sort(key=lambda pair: pair[0])
        return [node for _, node in found]


def _cos_deg(degrees: float) -> float:
    import math

    return max(0.05, math.cos(math.radians(degrees)))


def _tz_for(country: str | None, fallback: str | None = None) -> str | None:
    if fallback:
        return fallback
    if country:
        return COUNTRY_TZ.get(country.upper())
    return None


def _load_airports() -> tuple[list[Node], dict[str, Node]]:
    if not AIRPORTS_CSV.exists():
        raise FileNotFoundError(
            f"{AIRPORTS_CSV} mancante. Esegui: python scripts/fetch_datasets.py"
        )

    nodes: list[Node] = []
    by_iata: dict[str, Node] = {}
    with AIRPORTS_CSV.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("type") not in AIRPORT_TYPES:
                continue
            if not _truthy(row.get("scheduled_service")) and row.get(
                "scheduled_service"
            ) != "yes":
                continue
            iata = _clean(row.get("iata_code"))
            lat = _to_float(row.get("latitude_deg"))
            lon = _to_float(row.get("longitude_deg"))
            if not iata or lat is None or lon is None:
                continue
            country = _clean(row.get("iso_country"))
            node = Node(
                id=f"air:{iata}",
                name=_clean(row.get("name")) or iata,
                kind=NodeKind.AIRPORT,
                lat=lat,
                lon=lon,
                country=country,
                city=_clean(row.get("municipality")),
                timezone=_tz_for(country),
                iata=iata,
                provider_ids={},
                is_main=row.get("type") == "large_airport",
            )
            nodes.append(node)
            by_iata[iata] = node
    return nodes, by_iata


def _load_world_cities() -> tuple[list[CityEntry], dict[tuple[int, int], list[tuple[float, float, str]]]]:
    """Il gazetteer mondiale, e la griglia dei fusi orari che ne deriva.

    Senza questo file il mondo fuori dall'Europa non esisteva: il gazetteer
    Trainline copre 43 paesi europei, quindi «Tokyo» non si trovava affatto e
    «Londra» — che nel dataset europeo non c'e' — finiva per somiglianza su
    **Ondara**, in Spagna, senza che niente lo dicesse.

    Porta tre cose che servivano tutte e tre: le coordinate delle citta' del
    mondo, i loro nomi in altre lingue (gli esonimi italiani stanno li'), e il
    fuso orario, che OurAirports non pubblica e che senza questo file ogni
    aeroporto extra-europeo ereditava da `Europe/Rome`.

    Manca il file: si continua senza. L'Europa funziona lo stesso, e chi ha
    clonato il repository senza scaricare i dataset non deve vedere un errore
    al posto di una ricerca."""
    cities: list[CityEntry] = []
    griglia: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    if not WORLD_CITIES_TXT.exists():
        return cities, griglia

    with WORLD_CITIES_TXT.open(encoding="utf-8", newline="") as handle:
        for riga in handle:
            campi = riga.rstrip("\n").split("\t")
            if len(campi) <= GEONAMES_TIMEZONE:
                continue
            name = campi[GEONAMES_NAME].strip()
            lat = _to_float(campi[GEONAMES_LAT])
            lon = _to_float(campi[GEONAMES_LON])
            if not name or lat is None or lon is None:
                continue

            timezone = campi[GEONAMES_TIMEZONE].strip() or None
            if timezone:
                cella = (int(lat // TZ_GRID_STEP), int(lon // TZ_GRID_STEP))
                griglia.setdefault(cella, []).append((lat, lon, timezone))

            popolazione = _to_float(campi[GEONAMES_POPULATION]) or 0.0
            alias = []
            for grezzo in campi[GEONAMES_ALT_NAMES].split(","):
                if not grezzo.strip():
                    continue
                normalizzato = normalize(grezzo)
                # Dopo `normalize` gli accenti sono spariti, quindi «Köln» passa;
                # quello che resta non-ascii sono gli altri alfabeti, che nessuno
                # scrivera' mai nel campo di ricerca.
                if not normalizzato or not normalizzato.isascii():
                    continue
                alias.append(normalizzato)
                if len(alias) >= MAX_ALIASES:
                    break
            cities.append(
                CityEntry(
                    name=name,
                    normalized=normalize(name),
                    lat=lat,
                    lon=lon,
                    country=(campi[GEONAMES_COUNTRY].strip() or None),
                    # Fra due omonime vince la piu' grande, ed e' quasi sempre
                    # quella intesa: London (GB, 8,9 milioni) contro London
                    # (Canada, 422 mila). In scala logaritmica, altrimenti una
                    # metropoli schiaccerebbe qualunque altra cosa.
                    weight=max(1.0, math.log10(max(popolazione, 1.0)) - 3.0),
                    aliases=[a for a in dict.fromkeys(alias) if a],
                )
            )
    return cities, griglia


def _tz_da_griglia(
    griglia: dict[tuple[int, int], list[tuple[float, float, str]]],
    lat: float,
    lon: float,
) -> str | None:
    """Il fuso della citta' piu' vicina, cercata solo nelle celle attorno.

    Trentaquattromila citta' per tremila aeroporti sarebbero cento milioni di
    confronti a ogni avvio. Con la griglia se ne guardano poche decine."""
    base_lat, base_lon = int(lat // TZ_GRID_STEP), int(lon // TZ_GRID_STEP)
    # Prima le celle attorno; se non c'e' nessuna citta' — succede per gli scali
    # nel deserto, sulle isole e nel grande nord — si allarga invece di
    # arrendersi, perche' il ripiego e' `Europe/Rome` e su un volo alle Svalbard
    # sarebbe un orario sbagliato senza avviso.
    for raggio in (1, 3, 8):
        migliore: tuple[float, str] | None = None
        for dlat in range(-raggio, raggio + 1):
            for dlon in range(-raggio, raggio + 1):
                for city_lat, city_lon, timezone in griglia.get(
                    (base_lat + dlat, base_lon + dlon), ()
                ):
                    distanza = haversine_km(lat, lon, city_lat, city_lon)
                    if migliore is None or distanza < migliore[0]:
                        migliore = (distanza, timezone)
        if migliore is not None:
            return migliore[1]
    return None


def _load_stations(by_iata: dict[str, Node]) -> tuple[list[Node], list[CityEntry]]:
    if not STATIONS_CSV.exists():
        raise FileNotFoundError(
            f"{STATIONS_CSV} mancante. Esegui: python scripts/fetch_datasets.py"
        )

    nodes: list[Node] = []
    cities: list[CityEntry] = []

    # Il file usa il punto e virgola e contiene campi liberi molto lunghi.
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    with STATIONS_CSV.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        available = set(reader.fieldnames or [])
        missing = {"id", "name", "latitude", "longitude"} - available
        if missing:
            raise ValueError(
                f"stations.csv non ha le colonne attese, mancano: {sorted(missing)}"
            )
        provider_columns = {
            provider: (id_col, flag_col)
            for provider, (id_col, flag_col) in TRAINLINE_PROVIDER_COLUMNS.items()
            if id_col in available
        }

        for row in reader:
            lat = _to_float(row.get("latitude"))
            lon = _to_float(row.get("longitude"))
            name = _clean(row.get("name"))
            if lat is None or lon is None or not name:
                continue

            country = (_clean(row.get("country")) or "").upper() or None
            is_city = _truthy(row.get("is_city"))
            is_main = _truthy(row.get("is_main_station"))

            if is_city:
                cities.append(
                    CityEntry(
                        name=name,
                        normalized=normalize(name),
                        lat=lat,
                        lon=lon,
                        country=country,
                        weight=2.0,
                    )
                )
                # Le voci "citta'" di Trainline non sono fermate fisiche: servono
                # solo a posizionare la ricerca, non a costruire una gamba.
                continue

            provider_ids: dict[str, str] = {}
            for provider, (id_col, flag_col) in provider_columns.items():
                native = _clean(row.get(id_col))
                if not native:
                    continue
                if flag_col and flag_col in available and not _truthy(row.get(flag_col)):
                    continue
                provider_ids[provider] = native

            iata = _clean(row.get("iata_airport_code"))
            timezone = _tz_for(country, _clean(row.get("time_zone")))

            # Se e' un aeroporto gia' noto da OurAirports, non duplichiamo il nodo:
            # arricchiamo quello esistente con gli ID ferroviari/bus della fermata.
            if iata and iata in by_iata:
                target = by_iata[iata]
                target.provider_ids.update(provider_ids)
                if timezone and not target.timezone:
                    target.timezone = timezone
                continue

            if _truthy(row.get("is_airport")) and iata:
                kind = NodeKind.AIRPORT
            elif provider_ids.keys() & RAIL_PROVIDERS:
                kind = NodeKind.STATION
            elif provider_ids.keys() & BUS_PROVIDERS:
                kind = NodeKind.BUS_STOP
            else:
                kind = NodeKind.STATION

            nodes.append(
                Node(
                    id=f"tl:{row['id']}",
                    name=name,
                    kind=kind,
                    lat=lat,
                    lon=lon,
                    country=country,
                    city=None,
                    timezone=timezone,
                    iata=iata,
                    provider_ids=provider_ids,
                    is_main=is_main,
                )
            )

    return nodes, cities


def _load_overrides() -> tuple[list[Node], list[CityEntry]]:
    """Nodi e citta' curati a mano: porti, ferrovie locali, capolinea bus minori."""
    if not OVERRIDES_JSON.exists():
        return [], []

    payload = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
    nodes = [Node(**entry) for entry in payload.get("nodes", [])]
    for node in nodes:
        if not node.timezone:
            node.timezone = _tz_for(node.country)

    cities = [
        CityEntry(
            name=entry["name"],
            normalized=normalize(entry["name"]),
            lat=entry["lat"],
            lon=entry["lon"],
            country=entry.get("country"),
            weight=entry.get("weight", 3.0),
            aliases=[normalize(a) for a in entry.get("aliases", [])],
        )
        for entry in payload.get("cities", [])
    ]
    return nodes, cities


@lru_cache(maxsize=1)
def load_index() -> GeoIndex:
    """Costruisce l'indice una volta sola per processo."""
    world_cities, tz_grid = _load_world_cities()
    airports, by_iata = _load_airports()
    stations, cities = _load_stations(by_iata)
    extra_nodes, extra_cities = _load_overrides()

    # I fusi degli aeroporti fuori dai paesi noti: senza, un volo giapponese
    # veniva composto in `Europe/Rome` e mostrato con sette ore di scarto, senza
    # che niente lo segnalasse.
    if tz_grid:
        for node in airports:
            if node.timezone:
                continue
            node.timezone = _tz_da_griglia(tz_grid, node.lat, node.lon)

    nodes = airports + stations
    by_id = {node.id: node for node in nodes}

    # Gli override possono sia aggiungere nodi nuovi sia arricchire quelli esistenti
    # (es. attaccare l'ID FAL a una stazione gia' presente nel dataset Trainline).
    for node in extra_nodes:
        existing = by_id.get(node.id)
        if existing is not None:
            existing.provider_ids.update(node.provider_ids)
            existing.is_main = existing.is_main or node.is_main
            continue
        nodes.append(node)
        by_id[node.id] = node

    by_iata_all = {node.iata: node for node in nodes if node.iata}

    # L'ordine e' una precedenza: gli override sono correzioni scritte a mano,
    # le voci Trainline portano le coordinate tarate sulle stazioni, quelle
    # mondiali riempiono tutto il resto. Una citta' gia' presente con lo stesso
    # nome e lo stesso paese non si duplica: si prende solo i suoi nomi in
    # altre lingue, che sono la ragione per cui «Parigi» adesso si trova.
    all_cities = extra_cities + cities
    gia_note = {(city.normalized, city.country) for city in all_cities}
    per_chiave = {(city.normalized, city.country): city for city in all_cities}
    for city in world_cities:
        chiave = (city.normalized, city.country)
        if chiave in gia_note:
            esistente = per_chiave[chiave]
            esistente.aliases = list(dict.fromkeys([*esistente.aliases, *city.aliases]))
            continue
        all_cities.append(city)
        gia_note.add(chiave)
        per_chiave[chiave] = city

    by_provider: dict[str, dict[str, Node]] = {}
    for node in nodes:
        for provider, native_id in node.provider_ids.items():
            by_provider.setdefault(provider, {}).setdefault(str(native_id), node)

    logger.info(
        "indice geografico: %d nodi (%d aeroporti), %d citta' (%d dal mondo)",
        len(nodes),
        len(airports),
        len(all_cities),
        len(world_cities),
    )
    return GeoIndex(
        nodes=nodes,
        cities=all_cities,
        by_id=by_id,
        by_iata=by_iata_all,
        by_provider=by_provider,
    )
