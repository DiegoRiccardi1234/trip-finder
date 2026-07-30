"""Genera i percorsi candidati e ricompone le gambe in itinerari completi.

E' la parte che nessun operatore fa al posto tuo. Trenitalia sa dirti come
arrivare a Bari, FlixBus sa dirti se c'e' un pullman per Matera, ma solo qui si
scopre che sommandoli si arriva a Matera in nove ore invece che in venti.

Due responsabilita' distinte:

1. `plan()` decide **quali tratte vale la pena interrogare**. Su scala europea
   e' un problema di potatura: senza filtri si generano migliaia di combinazioni
   e la ricerca non finisce mai.
2. `assemble()` prende le gambe che sono tornate e le incastra, aggiungendo i
   trasferimenti, l'avvicinamento e l'ultimo miglio, e scartando le coincidenze
   che non stanno in piedi.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from app.config import get_settings
from app.geo.resolver import PlaceNotFound, Resolver
from app.models import Itinerary, Leg, Mode, Node, Place, SearchQuery, haversine_km
from app.routing import cost, feasibility
from app.routing.hub_graph import candidate_hubs

logger = logging.getLogger(__name__)

#: Quante combinazioni di gambe tenere vive mentre si costruisce un percorso.
MAX_PARTIALS = 80
#: Quante continuazioni provare per ogni combinazione parziale.
MAX_NEXT_PER_PARTIAL = 4
#: Tetto agli itinerari prodotti da un singolo percorso.
MAX_ITINERARIES_PER_PATH = 40


@dataclass(frozen=True)
class Waypoint:
    """Una tappa del percorso, con tutte le fermate utilizzabili in quel punto."""

    key: str
    label: str
    lat: float
    lon: float
    nodes: tuple[Node, ...]


@dataclass(frozen=True)
class Hop:
    origin: Waypoint
    destination: Waypoint

    @property
    def key(self) -> tuple[str, str]:
        return (self.origin.key, self.destination.key)

    @property
    def label(self) -> str:
        return f"{self.origin.label} -> {self.destination.label}"


@dataclass
class CandidatePath:
    waypoints: list[Waypoint]
    #: Da dove viene questo percorso: utile a spiegare le scelte in diagnostica.
    reason: str = "diretto"

    @property
    def hops(self) -> list[Hop]:
        return [
            Hop(a, b) for a, b in zip(self.waypoints, self.waypoints[1:])
        ]

    @property
    def label(self) -> str:
        return " -> ".join(point.label for point in self.waypoints)

    @property
    def key(self) -> str:
        return "|".join(point.key for point in self.waypoints)


def waypoint_from_place(place: Place, key: str) -> Waypoint:
    return Waypoint(
        key=key,
        label=place.label,
        lat=place.lat,
        lon=place.lon,
        nodes=tuple(place.nodes),
    )


def plan(
    origin: Place,
    destination: Place,
    query: SearchQuery,
    resolver: Resolver,
    extra_hubs: list[str] | None = None,
) -> list[CandidatePath]:
    """Percorsi da provare, dal piu' promettente."""
    settings = get_settings()
    start = waypoint_from_place(origin, "origin")
    end = waypoint_from_place(destination, "dest")

    paths: list[CandidatePath] = [CandidatePath([start, end], reason="diretto")]
    if query.max_changes <= 0:
        return paths

    hubs = candidate_hubs(
        origin.lat,
        origin.lon,
        destination.lat,
        destination.lon,
        modes=set(query.modes),
        max_detour_factor=settings.max_detour_factor,
        limit=6,
    )

    resolved: list[Waypoint] = []
    seen_hubs: set[str] = set()

    def add_hub(name: str, place: Place) -> None:
        # Un hub che coincide con un capo del viaggio non aggiunge nulla.
        if haversine_km(place.lat, place.lon, origin.lat, origin.lon) < 35:
            return
        if haversine_km(place.lat, place.lon, destination.lat, destination.lon) < 35:
            return
        if name in seen_hubs:
            return
        seen_hubs.add(name)
        resolved.append(waypoint_from_place(place, f"hub:{name}"))

    # I suggerimenti esterni (tipicamente dell'IA) entrano per primi: servono
    # proprio a coprire i casi che la geometria non vede. Passano dal matching
    # testuale perche' di loro conosciamo solo il nome.
    for name in extra_hubs or []:
        try:
            add_hub(name, resolver.resolve(name, modes=set(query.modes)))
        except PlaceNotFound:
            logger.debug("hub suggerito non risolto: %s", name)

    for hub in hubs:
        try:
            place = resolver.resolve_at(
                hub.lat, hub.lon, hub.name, hub.country, modes=set(query.modes)
            )
        except PlaceNotFound:
            logger.debug("hub senza fermate utilizzabili: %s", hub.name)
            continue
        add_hub(hub.name, place)

    for hub in resolved:
        paths.append(CandidatePath([start, hub, end], reason=f"via {hub.label}"))

    # Percorsi a due scali: costano molte richieste, quindi solo per viaggi lunghi
    # e solo combinando i due hub migliori, in ordine geografico coerente.
    direct_km = haversine_km(origin.lat, origin.lon, destination.lat, destination.lon)
    if query.max_changes >= 2 and direct_km > 600 and len(resolved) >= 2:
        for first in resolved[:3]:
            for second in resolved[:4]:
                if first.key == second.key:
                    continue
                to_first = haversine_km(origin.lat, origin.lon, first.lat, first.lon)
                to_second = haversine_km(origin.lat, origin.lon, second.lat, second.lon)
                if to_second <= to_first:
                    continue  # il secondo scalo deve stare piu' avanti del primo
                total = (
                    to_first
                    + haversine_km(first.lat, first.lon, second.lat, second.lon)
                    + haversine_km(second.lat, second.lon, destination.lat, destination.lon)
                )
                if total > direct_km * settings.max_detour_factor:
                    continue
                paths.append(
                    CandidatePath(
                        [start, first, second, end],
                        reason=f"via {first.label} e {second.label}",
                    )
                )

    unique: dict[str, CandidatePath] = {}
    for path in paths:
        unique.setdefault(path.key, path)
    return list(unique.values())[: settings.max_candidate_paths]


# --------------------------------------------------------------- ricomposizione


def _extend(partial: list[Leg], legs: list[Leg]) -> list[list[Leg]]:
    if not partial:
        return [[leg] for leg in legs]

    previous = partial[-1]
    usable = [leg for leg in legs if feasibility.connection_ok(previous, leg)]
    if not usable:
        return []

    # Fra tante coincidenze possibili tengono solo quelle interessanti: la piu'
    # rapida e la piu' economica. Le vie di mezzo non vincono mai.
    by_arrival = sorted(usable, key=lambda leg: leg.arrive)[:MAX_NEXT_PER_PARTIAL]
    by_price = sorted(
        usable, key=lambda leg: leg.fare.amount if leg.fare else float("inf")
    )[:MAX_NEXT_PER_PARTIAL]

    chosen: dict[int, Leg] = {}
    for leg in [*by_arrival, *by_price]:
        chosen[id(leg)] = leg
    return [[*partial, leg] for leg in chosen.values()]


def _combine(path: CandidatePath, legs_by_hop: dict[tuple[str, str], list[Leg]]) -> list[list[Leg]]:
    partials: list[list[Leg]] = [[]]
    for hop in path.hops:
        legs = legs_by_hop.get(hop.key) or []
        if not legs:
            return []
        grown: list[list[Leg]] = []
        for partial in partials:
            grown.extend(_extend(partial, legs))
        if not grown:
            return []
        # Potatura: si tengono le combinazioni che arrivano prima e quelle che
        # costano meno, che sono le uniche candidate a vincere alla fine.
        grown.sort(key=lambda combo: (combo[-1].arrive, _price(combo)))
        partials = grown[:MAX_PARTIALS]
    return partials


def _price(legs: list[Leg]) -> float:
    return sum(leg.fare.amount for leg in legs if leg.fare)


def _itinerary_id(legs: list[Leg]) -> str:
    raw = "|".join(
        f"{leg.provider}:{leg.origin.id}:{leg.destination.id}:{leg.depart.isoformat()}"
        for leg in legs
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def assemble(
    path: CandidatePath,
    legs_by_hop: dict[tuple[str, str], list[Leg]],
    origin: Place,
    destination: Place,
    query: SearchQuery,
) -> list[Itinerary]:
    """Da gambe sciolte a itinerari completi, trasferimenti inclusi."""
    itineraries: list[Itinerary] = []

    for combo in _combine(path, legs_by_hop)[:MAX_ITINERARIES_PER_PATH]:
        # Un itinerario che comincia o finisce su un'isola raggiungibile solo
        # via mare non si puo' completare con una stima su strada: senza questo
        # controllo il motore proporrebbe un autobus da Trapani a Levanzo.
        if not feasibility.land_link_possible(origin, combo[0].origin):
            continue
        if not feasibility.land_link_possible(destination, combo[-1].destination):
            continue

        full: list[Leg] = []

        approach = feasibility.first_mile(origin, combo[0])
        if approach is not None:
            full.append(approach)

        for index, leg in enumerate(combo):
            if index > 0:
                transfer = feasibility.transfer_between(combo[index - 1], leg)
                if transfer is not None:
                    full.append(transfer)
            full.append(leg)

        final = feasibility.last_mile(destination, combo[-1])
        if final is not None:
            full.append(final)

        itineraries.append(
            Itinerary(
                id=_itinerary_id(combo),
                legs=full,
                cost=cost.compute(full, query),
            )
        )

    return itineraries


def deduplicate(itineraries: list[Itinerary]) -> list[Itinerary]:
    """Percorsi diversi possono produrre lo stesso viaggio: se ne tiene uno."""
    seen: dict[str, Itinerary] = {}
    for itinerary in itineraries:
        seen.setdefault(itinerary.id, itinerary)
    return list(seen.values())


def tasks_for_tier(
    paths: list[CandidatePath], query: SearchQuery, tier: int
) -> list[tuple[tuple[str, str], object, Node, Node]]:
    """Tutti i compiti di un livello, gia' deduplicati fra i percorsi visibili."""
    visible = paths_for_tier(paths, tier)
    tiers = {1} if tier <= 1 else {tier}
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[tuple[str, str], object, Node, Node]] = []
    for hop_key, hop in required_hops(visible).items():
        for provider, origin_node, dest_node in provider_tasks(hop, query, tiers=tiers):
            key = provider.route_key(origin_node, dest_node)  # type: ignore[attr-defined]
            if key in seen:
                continue
            seen.add(key)
            out.append((hop_key, provider, origin_node, dest_node))
    return out


def required_hops(paths: list[CandidatePath]) -> dict[tuple[str, str], Hop]:
    """Tratte distinte da interrogare, deduplicate fra tutti i percorsi.

    Torino-Bari compare sia nel percorso via Bari sia in quello via Bari e
    Napoli: va interrogata una volta sola."""
    hops: dict[tuple[str, str], Hop] = {}
    for path in paths:
        for hop in path.hops:
            hops.setdefault(hop.key, hop)
    return hops


#: Quante fermate provare per lato e per operatore. Alzarlo fa esplodere il
#: numero di richieste senza aggiungere soluzioni: due stazioni principali della
#: stessa citta' restituiscono quasi sempre gli stessi treni.
MAX_NODES_PER_SIDE = 2
#: Su uno scalo intermedio ne basta una. All'origine e alla destinazione le
#: fermate contano davvero (partire da Porta Nuova o da Porta Susa cambia il
#: viaggio); in mezzo no, si scende e si risale nella stessa citta'. Tenerne due
#: per ogni hub raddoppiava le richieste al ferroviario, che e' l'operatore piu'
#: lento e quello con piu' combinazioni.
MAX_NODES_PER_HUB = 1
#: L'aereo li vede tutti. Due stazioni della stessa citta' danno quasi sempre
#: gli stessi treni, due aeroporti no: da Cuneo si vola dove da Torino non si
#: vola, e viceversa. Escluderne uno significa togliere un'occasione senza
#: dirlo. Costa poco perche' la copertura per scalo e' in cache e una coppia
#: non servita esce senza nemmeno una richiesta di rete. Il tetto vero e'
#: quello del resolver, che e' solo una rete di sicurezza.
MAX_NODES_PER_SIDE_AIR = 8


def _usable_nodes(provider, nodes: tuple[Node, ...], limit: int) -> list[Node]:
    from app.models import MODE_NODE_KINDS

    kinds = MODE_NODE_KINDS.get(provider.mode, frozenset())
    usable = [node for node in nodes if node.kind in kinds and provider.supports_node(node)]
    # Le fermate principali per prime: sono quelle con piu' collegamenti.
    usable.sort(key=lambda node: (not node.is_main, -len(node.provider_ids)))
    return usable[:limit]


def _side_limit(waypoint: Waypoint, mode: Mode | None = None) -> int:
    if waypoint.key not in ("origin", "dest"):
        return MAX_NODES_PER_HUB
    return MAX_NODES_PER_SIDE_AIR if mode is Mode.AIR else MAX_NODES_PER_SIDE


def paths_for_tier(paths: list[CandidatePath], tier: int) -> list[CandidatePath]:
    """Percorsi che un adapter di quel livello ha diritto di vedere.

    Gli adapter veloci li vedono tutti. Quelli lenti no: aprire un browser per
    ogni tratta di ogni percorso a due scali costerebbe minuti. Si tengono il
    diretto e i primi scali, che sono ordinati per promessa, e li' le soluzioni
    vere si trovano comunque."""
    if tier <= 1:
        return paths
    limit = get_settings().max_paths_for_slow_providers
    short = [path for path in paths if len(path.waypoints) <= 3]
    return short[:limit]


def provider_tasks(
    hop: Hop, query: SearchQuery, tiers: set[int] | None = None
) -> list[tuple[object, Node, Node]]:
    """Coppie (provider, nodo, nodo) da interrogare per una tratta.

    Il costo di una ricerca e' dominato dal numero di richieste, non dalla loro
    durata. Qui si taglia in tre punti: si considerano solo i livelli richiesti,
    si prendono al massimo due fermate per lato, e si deduplica per `route_key`,
    cosi' un operatore che ragiona per citta' come FlixBus viene interrogato una
    volta e non una per fermata."""
    from app.providers import registry

    tasks: dict[tuple[str, str, str], tuple[object, Node, Node]] = {}
    for provider in registry.all_providers():
        if provider.mode not in query.modes:
            continue
        if tiers is not None and provider.tier not in tiers:
            continue
        origins = _usable_nodes(
            provider, hop.origin.nodes, _side_limit(hop.origin, provider.mode)
        )
        destinations = _usable_nodes(
            provider, hop.destination.nodes, _side_limit(hop.destination, provider.mode)
        )
        for origin_node in origins:
            for dest_node in destinations:
                if not provider.can_serve(origin_node, dest_node):
                    continue
                tasks.setdefault(
                    provider.route_key(origin_node, dest_node),
                    (provider, origin_node, dest_node),
                )
    return list(tasks.values())


@dataclass
class PlanSummary:
    """Cosa e' stato deciso di interrogare, per mostrarlo nella UI."""

    paths: list[CandidatePath] = field(default_factory=list)
    hops: dict[tuple[str, str], Hop] = field(default_factory=dict)

    @property
    def as_dict(self) -> dict:
        return {
            "paths": [
                {"label": path.label, "reason": path.reason, "hops": len(path.hops)}
                for path in self.paths
            ],
            "hops": [hop.label for hop in self.hops.values()],
        }
