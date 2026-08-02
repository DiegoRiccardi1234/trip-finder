"""Hub candidati come punto di cambio.

Su scala continentale non si puo' provare ogni citta' come scalo: il numero di
richieste esploderebbe e il 99% sarebbe assurdo (Torino-Matera via Amburgo).
Il filtro e' geometrico prima che di reputazione: un hub e' ammissibile solo se
il percorso spezzato origine-hub-destinazione non allunga oltre una certa soglia
il percorso diretto. Fra quelli ammissibili si preferiscono i nodi grandi, che
hanno piu' collegamenti e piu' probabilita' di produrre una soluzione vera.

Gli hub sono di due specie, e la differenza conta.

**Quelli scritti a mano, in Europa.** I loro pesi non sono la popolazione: sono
quanto quel posto e' utile *come scalo*. Bologna pesa piu' di quanto suggerisca
la sua taglia perche' e' lo snodo di tutta la rete ferroviaria italiana. Questo
si sa e non si deduce da un dataset.

**Quelli ricavati dal gazetteer, nel resto del mondo.** Erano ottantotto citta'
europee e basta: su una tratta asiatica o americana l'elenco degli ammissibili
restava vuoto e restavano solo i voli diretti — Tokyo-Lima senza scalo non
esiste, quindi non usciva niente. Le citta' grandi del mondo con un aeroporto
vicino diventano scali **solo per l'aereo**: fuori Europa non c'e' un solo
adapter ferroviario o di pullman, e proporre un cambio treno a Nagoya
significherebbe generare richieste che nessuno puo' soddisfare.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.models import Mode, haversine_km


@dataclass(frozen=True)
class Hub:
    name: str
    lat: float
    lon: float
    country: str
    #: 3 = snodo continentale, 2 = snodo nazionale, 1 = utile in zona.
    weight: int
    modes: frozenset[Mode]


ALL = frozenset({Mode.RAIL, Mode.BUS, Mode.AIR})
RAIL_BUS = frozenset({Mode.RAIL, Mode.BUS})
ALL_FERRY = frozenset({Mode.RAIL, Mode.BUS, Mode.AIR, Mode.FERRY})

HUBS: tuple[Hub, ...] = (
    # --- Italia: fitta, perche' e' il territorio che serve piu' spesso ---
    Hub("Milano", 45.4642, 9.1900, "IT", 3, ALL),
    Hub("Roma", 41.9028, 12.4964, "IT", 3, ALL),
    Hub("Bologna", 44.4949, 11.3426, "IT", 3, ALL),
    Hub("Napoli", 40.8518, 14.2681, "IT", 3, ALL_FERRY),
    Hub("Firenze", 43.7696, 11.2558, "IT", 2, ALL),
    Hub("Torino", 45.0703, 7.6869, "IT", 2, ALL),
    Hub("Venezia", 45.4408, 12.3155, "IT", 2, ALL_FERRY),
    Hub("Verona", 45.4384, 10.9916, "IT", 2, ALL),
    Hub("Genova", 44.4056, 8.9463, "IT", 2, ALL_FERRY),
    Hub("Bari", 41.1171, 16.8719, "IT", 2, ALL_FERRY),
    Hub("Salerno", 40.6824, 14.7681, "IT", 2, ALL_FERRY),
    Hub("Foggia", 41.4622, 15.5446, "IT", 1, RAIL_BUS),
    Hub("Pescara", 42.4618, 14.2161, "IT", 1, ALL),
    Hub("Ancona", 43.6158, 13.5189, "IT", 1, ALL_FERRY),
    Hub("Pisa", 43.7228, 10.4017, "IT", 1, ALL),
    Hub("Potenza", 40.6418, 15.8056, "IT", 1, RAIL_BUS),
    Hub("Taranto", 40.4644, 17.2470, "IT", 1, RAIL_BUS),
    Hub("Brindisi", 40.6327, 17.9418, "IT", 1, ALL_FERRY),
    Hub("Lamezia Terme", 38.9058, 16.2670, "IT", 1, ALL),
    Hub("Reggio Calabria", 38.1105, 15.6613, "IT", 1, ALL_FERRY),
    Hub("Catania", 37.5079, 15.0830, "IT", 1, ALL_FERRY),
    Hub("Palermo", 38.1157, 13.3615, "IT", 1, ALL_FERRY),
    Hub("Cagliari", 39.2238, 9.1217, "IT", 1, ALL_FERRY),
    Hub("Bolzano", 46.4983, 11.3548, "IT", 1, RAIL_BUS),
    Hub("Trieste", 45.6495, 13.7768, "IT", 1, ALL_FERRY),
    # --- Francia ---
    Hub("Paris", 48.8566, 2.3522, "FR", 3, ALL),
    Hub("Lyon", 45.7640, 4.8357, "FR", 2, ALL),
    Hub("Marseille", 43.2965, 5.3698, "FR", 2, ALL_FERRY),
    Hub("Nice", 43.7102, 7.2620, "FR", 2, ALL_FERRY),
    Hub("Toulouse", 43.6047, 1.4442, "FR", 1, ALL),
    Hub("Bordeaux", 44.8378, -0.5792, "FR", 1, ALL),
    Hub("Lille", 50.6292, 3.0573, "FR", 1, ALL),
    Hub("Strasbourg", 48.5734, 7.7521, "FR", 1, ALL),
    # --- Germania, Austria, Svizzera ---
    Hub("Frankfurt", 50.1109, 8.6821, "DE", 3, ALL),
    Hub("München", 48.1351, 11.5820, "DE", 3, ALL),
    Hub("Berlin", 52.5200, 13.4050, "DE", 3, ALL),
    Hub("Köln", 50.9375, 6.9603, "DE", 2, ALL),
    Hub("Hamburg", 53.5511, 9.9937, "DE", 2, ALL_FERRY),
    Hub("Stuttgart", 48.7758, 9.1829, "DE", 2, ALL),
    Hub("Düsseldorf", 51.2277, 6.7735, "DE", 2, ALL),
    Hub("Nürnberg", 49.4521, 11.0767, "DE", 1, ALL),
    Hub("Zürich", 47.3769, 8.5417, "CH", 3, ALL),
    Hub("Genève", 46.2044, 6.1432, "CH", 2, ALL),
    Hub("Basel", 47.5596, 7.5886, "CH", 2, ALL),
    Hub("Wien", 48.2082, 16.3738, "AT", 3, ALL),
    Hub("Salzburg", 47.8095, 13.0550, "AT", 1, ALL),
    Hub("Innsbruck", 47.2692, 11.4041, "AT", 1, RAIL_BUS),
    # --- Penisola iberica ---
    Hub("Madrid", 40.4168, -3.7038, "ES", 3, ALL),
    Hub("Barcelona", 41.3874, 2.1686, "ES", 3, ALL_FERRY),
    Hub("Valencia", 39.4699, -0.3763, "ES", 2, ALL_FERRY),
    Hub("Sevilla", 37.3891, -5.9845, "ES", 1, ALL),
    Hub("Bilbao", 43.2630, -2.9350, "ES", 1, ALL),
    Hub("Lisboa", 38.7223, -9.1393, "PT", 2, ALL_FERRY),
    Hub("Porto", 41.1579, -8.6291, "PT", 1, ALL),
    # --- Benelux, Regno Unito, Irlanda ---
    Hub("Bruxelles", 50.8503, 4.3517, "BE", 3, ALL),
    Hub("Amsterdam", 52.3676, 4.9041, "NL", 3, ALL_FERRY),
    Hub("Rotterdam", 51.9244, 4.4777, "NL", 1, ALL_FERRY),
    Hub("Luxembourg", 49.6116, 6.1319, "LU", 1, ALL),
    Hub("London", 51.5074, -0.1278, "GB", 3, ALL_FERRY),
    Hub("Manchester", 53.4808, -2.2426, "GB", 2, ALL),
    Hub("Edinburgh", 55.9533, -3.1883, "GB", 1, ALL),
    Hub("Dublin", 53.3498, -6.2603, "IE", 2, ALL_FERRY),
    # --- Nord Europa ---
    Hub("København", 55.6761, 12.5683, "DK", 2, ALL_FERRY),
    Hub("Stockholm", 59.3293, 18.0686, "SE", 2, ALL_FERRY),
    Hub("Oslo", 59.9139, 10.7522, "NO", 2, ALL_FERRY),
    Hub("Helsinki", 60.1699, 24.9384, "FI", 1, ALL_FERRY),
    # --- Europa centro-orientale e balcanica ---
    Hub("Praha", 50.0755, 14.4378, "CZ", 2, ALL),
    Hub("Warszawa", 52.2297, 21.0122, "PL", 2, ALL),
    Hub("Kraków", 50.0647, 19.9450, "PL", 1, ALL),
    Hub("Budapest", 47.4979, 19.0402, "HU", 2, ALL),
    Hub("Bratislava", 48.1486, 17.1077, "SK", 1, ALL),
    Hub("Ljubljana", 46.0569, 14.5058, "SI", 1, RAIL_BUS),
    Hub("Zagreb", 45.8150, 15.9819, "HR", 1, ALL),
    Hub("Beograd", 44.7866, 20.4489, "RS", 1, ALL),
    Hub("București", 44.4268, 26.1025, "RO", 2, ALL),
    Hub("Sofia", 42.6977, 23.3219, "BG", 1, ALL),
    Hub("Athina", 37.9838, 23.7275, "GR", 2, ALL_FERRY),
    Hub("Thessaloniki", 40.6401, 22.9444, "GR", 1, ALL_FERRY),
    Hub("Istanbul", 41.0082, 28.9784, "TR", 2, ALL_FERRY),
)


#: Popolazione oltre la quale una citta' del mondo puo' fare da scalo. Un
#: milione non e' una soglia di prestigio: sotto, l'aeroporto quasi sempre non
#: ha collegamenti che facciano di quella citta' un punto di cambio.
MIN_POP_HUB = 1_000_000
#: Quanto lontano puo' stare l'aeroporto dalla citta' perche' conti come suo.
#: Sessanta chilometri copre i casi veri (Narita sta a 60 da Tokyo).
HUB_AIRPORT_KM = 60.0
#: I paesi gia' coperti dalla lista curata: li' i pesi scritti a mano valgono
#: piu' di qualunque conteggio di abitanti.
PAESI_CURATI = frozenset(hub.country for hub in HUBS)


@lru_cache(maxsize=1)
def world_hubs() -> tuple[Hub, ...]:
    """Scali ricavati dal gazetteer, fuori dai paesi gia' curati a mano.

    Senza i dataset non ce ne sono, e non e' un guasto: l'Europa continua a
    funzionare con la lista scritta, che e' quella che serve piu' spesso."""
    try:
        from app.geo.datasets import load_index
    except ImportError:  # pragma: no cover - solo se il modulo sparisse
        return ()

    try:
        index = load_index()
    except FileNotFoundError:
        return ()

    # Gli aeroporti in una griglia di un grado: per ogni citta' si guardano
    # nove celle invece di tremila scali.
    griglia: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for node in index.nodes:
        if node.iata:
            griglia.setdefault((int(node.lat), int(node.lon)), []).append((node.lat, node.lon))

    def ha_aeroporto(lat: float, lon: float) -> bool:
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                for air_lat, air_lon in griglia.get((int(lat) + dlat, int(lon) + dlon), ()):
                    if haversine_km(lat, lon, air_lat, air_lon) <= HUB_AIRPORT_KM:
                        return True
        return False

    solo_aereo = frozenset({Mode.AIR})
    hubs: list[Hub] = []
    for city in index.cities:
        if city.country in PAESI_CURATI or not city.country:
            continue
        # `weight` del gazetteer e' log10(popolazione) - 3: un milione fa 3.
        if city.weight < 3.0 or not ha_aeroporto(city.lat, city.lon):
            continue
        hubs.append(
            Hub(
                name=city.name,
                lat=city.lat,
                lon=city.lon,
                country=city.country,
                weight=3 if city.weight >= 3.7 else 2,  # cinque milioni
                modes=solo_aereo,
            )
        )
    return tuple(hubs)


def candidate_hubs(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    modes: set[Mode],
    max_detour_factor: float = 1.4,
    limit: int = 8,
) -> list[Hub]:
    """Hub ammissibili fra due punti, dal piu' promettente.

    Ammissibile significa: serve almeno uno dei modi richiesti, non coincide di
    fatto con l'origine o la destinazione, e non allunga il viaggio oltre la
    soglia di detour."""
    direct = haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
    if direct < 120:
        # Sotto questa distanza un cambio non compra mai nulla.
        return []

    budget = direct * max_detour_factor
    # Un hub troppo vicino a un capo del viaggio non e' un cambio, e' la stessa
    # citta' con un altro nome. La soglia va tenuta bassa: Bari dista appena
    # 55 km da Matera ed e' esattamente l'hub che rende possibile quel viaggio,
    # perche' Matera non ha ne' aeroporto ne' ferrovia nazionale.
    min_leg = min(40.0, direct * 0.08)

    admissible: list[tuple[float, float, float, Hub]] = []
    for hub in (*HUBS, *world_hubs()):
        if not hub.modes & modes:
            continue
        to_hub = haversine_km(origin_lat, origin_lon, hub.lat, hub.lon)
        from_hub = haversine_km(hub.lat, hub.lon, dest_lat, dest_lon)
        if to_hub < min_leg or from_hub < min_leg:
            continue
        total = to_hub + from_hub
        if total > budget:
            continue
        # Meglio un hub grande e poco fuori strada: il detour relativo pesa
        # quanto il peso dell'hub, cosi' nessuno dei due domina l'altro.
        detour_ratio = total / direct
        score = hub.weight * 1.0 - (detour_ratio - 1.0) * 3.0
        admissible.append((score, to_hub, from_hub, hub))

    if not admissible:
        return []

    picked: list[Hub] = []
    seen: set[str] = set()

    def take(entries: list[tuple[float, float, float, Hub]], count: int) -> None:
        for entry in entries[:count]:
            hub = entry[3]
            if hub.name not in seen:
                seen.add(hub.name)
                picked.append(hub)

    # I due hub piu' vicini alla destinazione entrano sempre. E' la regola che
    # rende trovabile Torino-Matera: Bari e' lo scalo obbligato per Matera, che
    # non ha ne' aeroporto ne' ferrovia nazionale, ma per pura geometria perde
    # contro citta' come Genova che stanno "sulla linea" e non servono a nulla.
    # Lo stesso vale a specchio per la partenza da un posto mal collegato.
    take(sorted(admissible, key=lambda entry: entry[2]), 2)
    take(sorted(admissible, key=lambda entry: entry[1]), 1)
    take(sorted(admissible, key=lambda entry: entry[0], reverse=True), len(admissible))

    return picked[:limit]
