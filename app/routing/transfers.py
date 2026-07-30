"""Stima di tempo e costo degli spostamenti che nessun operatore ti vende.

Sono i pezzi che gli aggregatori tipicamente ignorano e che cambiano il verdetto:
il collegamento aeroporto-citta', il cambio di stazione dentro la stessa citta',
l'ultimo miglio da Bari Palese a Matera. Un volo "a 19 euro" che richiede
un'ora e mezza di navetta a 8 euro non e' un volo a 19 euro.

Le stime sono volutamente conservative e sempre marcate come tali: meglio dire
"circa 9 euro, stimati" che spacciare per esatto un numero inventato.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Mode, Node, NodeKind, node_distance_km


@dataclass(frozen=True)
class Transfer:
    duration_min: int
    price_eur: float
    description: str
    estimated: bool = True


#: Collegamenti aeroporto-centro citta' verificati, per gli scali piu' usati.
#: Chiave IATA. Dove non c'e' voce si usa la formula generica.
AIRPORT_LINKS: dict[str, Transfer] = {
    "TRN": Transfer(45, 3.0, "Torino Caselle: bus Arriva per Porta Nuova", False),
    "MXP": Transfer(50, 13.0, "Malpensa Express per Milano Cadorna/Centrale", False),
    "LIN": Transfer(25, 2.2, "Linate: metro M4 per Milano centro", False),
    "BGY": Transfer(50, 10.0, "Orio al Serio: bus per Milano Centrale", False),
    "BLQ": Transfer(20, 12.2, "Marconi Express per Bologna Centrale", False),
    "FCO": Transfer(32, 14.0, "Leonardo Express per Roma Termini", False),
    "CIA": Transfer(40, 6.0, "Ciampino: bus per Roma Termini", False),
    "NAP": Transfer(20, 5.0, "Capodichino: Alibus per Napoli Centrale", False),
    "BRI": Transfer(20, 5.0, "Bari Palese: treno per Bari Centrale", False),
    "PSR": Transfer(15, 1.5, "Pescara: bus per la stazione centrale", False),
    "VCE": Transfer(25, 10.0, "Venezia Tessera: bus ATVO per Mestre", False),
    "PSA": Transfer(8, 5.0, "Pisa Mover per Pisa Centrale", False),
    "CTA": Transfer(25, 4.0, "Catania Fontanarossa: Alibus per il centro", False),
    "PMO": Transfer(50, 6.5, "Punta Raisi: Trinacria Express per Palermo", False),
    "CAG": Transfer(10, 1.3, "Cagliari Elmas: treno per la stazione centrale", False),
    "SUF": Transfer(10, 1.5, "Lamezia Terme: treno per la stazione centrale", False),
    "CDG": Transfer(45, 11.8, "RER B per Parigi centro", False),
    "ORY": Transfer(35, 14.0, "Orlyval + RER per Parigi centro", False),
    "BCN": Transfer(35, 5.2, "Aerobus per Plaça Catalunya", False),
    "MAD": Transfer(30, 5.0, "Metro linea 8 per Nuevos Ministerios", False),
    "FRA": Transfer(15, 5.8, "S-Bahn per Frankfurt Hauptbahnhof", False),
    "MUC": Transfer(45, 13.5, "S-Bahn S1/S8 per München Hauptbahnhof", False),
    "STN": Transfer(50, 22.0, "Stansted Express per London Liverpool Street", False),
    "LTN": Transfer(45, 17.0, "DART + treno per London St Pancras", False),
}

#: Corse curate fra un aeroporto e una citta' che non e' la sua.
#: Il caso classico: chi va a Matera atterra a Bari e prosegue in pullman.
AIRPORT_TO_CITY: dict[tuple[str, str], Transfer] = {
    ("BRI", "matera"): Transfer(
        75, 6.0, "Pugliairbus Bari Aeroporto - Matera (Ferrovie Appulo Lucane)", False
    ),
    ("BRI", "altamura"): Transfer(70, 6.0, "bus Bari Aeroporto - Altamura", True),
    ("BDS", "lecce"): Transfer(45, 7.0, "bus Brindisi Aeroporto - Lecce", False),
    ("NAP", "salerno"): Transfer(70, 10.0, "bus Capodichino - Salerno", False),
}

#: Velocita' media e costo dei mezzi di superficie usati per le stime generiche.
URBAN_SPEED_KMH = 22.0
REGIONAL_SPEED_KMH = 48.0
FIXED_WAIT_MIN = 12
PRICE_PER_KM = 0.13
MIN_PRICE = 1.8


#: Quanto lontano da un porto puo' stare un punto raggiungibile via terra.
#: Oltre questa soglia, con un porto in mezzo, il collegamento e' quasi certo
#: via mare e non si puo' stimare su strada.
PORT_LAND_RADIUS_KM = 12.0


def reachable_by_land(origin: Node, destination: Node) -> bool:
    """Se ha senso stimare un collegamento di superficie fra due punti.

    Serve per le isole. Il motore trovava un traghetto Levanzo-Favignana e ci
    attaccava davanti un "avvicinamento" su strada da Trapani a Levanzo, che e'
    un'isola in mezzo al mare: quaranta minuti di autobus inesistente. Fra due
    porti distinti il collegamento e' via mare per definizione e deve venire da
    un operatore vero, non da una stima."""
    kinds = {origin.kind, destination.kind}
    if NodeKind.PORT not in kinds:
        return True
    distance = node_distance_km(origin, destination)
    if kinds == {NodeKind.PORT}:
        # Due porti diversi: solo se sono di fatto lo stesso scalo.
        return distance <= 2.0
    return distance <= PORT_LAND_RADIUS_KM


def city_of(node: Node) -> str:
    if node.city:
        return node.city.strip().lower()
    return node.name.split(",")[0].split("(")[0].strip().lower()


def estimate(origin: Node, destination: Node, destination_label: str | None = None) -> Transfer:
    """Tempo e costo per andare da un nodo a un altro con mezzi di superficie."""
    if origin.id == destination.id:
        return Transfer(0, 0.0, "stessa fermata", estimated=False)

    distance_km = node_distance_km(origin, destination)
    target_city = (destination_label or city_of(destination)).strip().lower()

    # Corsa dedicata nota fra questo aeroporto e questa citta'.
    if origin.kind is NodeKind.AIRPORT and origin.iata:
        curated = AIRPORT_TO_CITY.get((origin.iata, target_city))
        if curated:
            return curated
        # Collegamento standard aeroporto-centro: vale solo se la destinazione
        # e' davvero la citta' dell'aeroporto, non una a settanta chilometri.
        link = AIRPORT_LINKS.get(origin.iata)
        if link and distance_km <= 35:
            return link

    if destination.kind is NodeKind.AIRPORT and destination.iata:
        link = AIRPORT_LINKS.get(destination.iata)
        if link and distance_km <= 35:
            return Transfer(
                link.duration_min, link.price_eur, link.description, link.estimated
            )

    # Su strada si percorre piu' della linea d'aria.
    road_km = distance_km * 1.3
    speed = URBAN_SPEED_KMH if road_km <= 15 else REGIONAL_SPEED_KMH
    duration = FIXED_WAIT_MIN + int(road_km / speed * 60)
    price = max(MIN_PRICE, round(road_km * PRICE_PER_KM, 2))
    return Transfer(
        duration_min=duration,
        price_eur=price,
        description=f"collegamento stimato di {road_km:.0f} km con mezzi pubblici",
        estimated=True,
    )


#: Minuti da lasciare fra l'arrivo e la partenza successiva, per modo.
#: Per i voli il vincolo non e' scendere dall'aereo, e' il check-in del volo dopo.
MIN_CONNECTION_SAME_NODE: dict[tuple[Mode, Mode], int] = {
    (Mode.RAIL, Mode.RAIL): 10,
    (Mode.RAIL, Mode.BUS): 15,
    (Mode.BUS, Mode.RAIL): 15,
    (Mode.BUS, Mode.BUS): 15,
    (Mode.AIR, Mode.AIR): 75,
    (Mode.AIR, Mode.RAIL): 45,
    (Mode.AIR, Mode.BUS): 45,
    (Mode.RAIL, Mode.AIR): 110,
    (Mode.BUS, Mode.AIR): 110,
    (Mode.FERRY, Mode.RAIL): 45,
    (Mode.FERRY, Mode.BUS): 45,
    (Mode.RAIL, Mode.FERRY): 60,
    (Mode.BUS, Mode.FERRY): 60,
    (Mode.AIR, Mode.FERRY): 90,
    (Mode.FERRY, Mode.AIR): 120,
    (Mode.FERRY, Mode.FERRY): 60,
}
DEFAULT_MIN_CONNECTION = 30


def min_connection_minutes(
    arriving_mode: Mode, departing_mode: Mode, same_node: bool, separate_tickets: bool
) -> int:
    """Margine minimo raccomandato fra due gambe."""
    base = MIN_CONNECTION_SAME_NODE.get(
        (arriving_mode, departing_mode), DEFAULT_MIN_CONNECTION
    )
    if not same_node:
        base += 15  # spostarsi fra due fermate ha sempre un margine di incertezza
    if separate_tickets:
        # Senza protezione, un ritardo non ti fa riproteggere: serve piu' cuscinetto.
        base = int(base * 1.5)
    return base
