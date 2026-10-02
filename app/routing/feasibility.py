"""Verifica che due gambe si possano davvero incastrare, e cosa costa farlo.

Il punto delicato: una coincidenza sulla carta non e' una coincidenza reale.
Se arrivi a Bari Centrale alle 17:27 e il pullman parte da Largo Sorrentino alle
17:40, quei tredici minuti includono uno spostamento a piedi e nessun margine.
Se poi i due biglietti sono separati, un ritardo di dieci minuti ti lascia a
terra senza rimborso. Qui si decide cosa e' proponibile e cosa no.
"""

from __future__ import annotations

from datetime import timedelta, timezone

from app.models import Leg, Mode, Node, NodeKind, Place, node_distance_km
from app.routing import transfers

#: Oltre questo tempo di attesa una coincidenza smette di essere un viaggio
#: sensato e diventa una giornata persa in stazione.
MAX_WAIT_MINUTES = 8 * 60

#: Sotto questa distanza due fermate si considerano la stessa: il trasferimento
#: e' una camminata, non un mezzo da prendere.
SAME_NODE_KM = 0.6

#: Due moli entro questa distanza sono lo stesso porto. I cataloghi degli
#: operatori e il nostro indice mettono il punto in posti leggermente diversi.
SAME_PORT_KM = 6.0


def separate_tickets(prev: Leg, nxt: Leg) -> bool:
    """Due gambe sono protette solo se le vende lo stesso operatore."""
    return prev.provider != nxt.provider


def required_margin(prev: Leg, nxt: Leg) -> int:
    same_node = (
        prev.destination.id == nxt.origin.id
        or node_distance_km(prev.destination, nxt.origin) <= SAME_NODE_KM
    )
    base = transfers.min_connection_minutes(
        prev.mode, nxt.mode, same_node, separate_tickets(prev, nxt)
    )
    if not same_node:
        base += transfers.estimate(prev.destination, nxt.origin).duration_min
    return base


def gap_minutes(prev: Leg, nxt: Leg) -> int:
    elapsed = nxt.depart.astimezone(timezone.utc) - prev.arrive.astimezone(timezone.utc)
    return int(elapsed.total_seconds() // 60)


def connection_ok(prev: Leg, nxt: Leg) -> bool:
    gap = gap_minutes(prev, nxt)
    if gap > MAX_WAIT_MINUTES:
        return False
    return gap >= required_margin(prev, nxt)


def is_island(place: Place) -> bool:
    """Vero se a quella localita' si arriva solo via mare.

    Il segnale e' nei dati, non in una lista scritta a mano: se fra le fermate
    utilizzabili ci sono solo porti, non c'e' modo di arrivarci su strada."""
    kinds = {node.kind for node in place.nodes}
    return bool(kinds) and kinds <= {NodeKind.PORT}


def land_link_possible(place: Place, node: Node) -> bool:
    """Se il collegamento fra una localita' e una fermata e' stimabile a terra.

    Due casi distinti, ed e' il secondo quello che sfuggiva. Il primo: la
    fermata e' un porto lontano, e allora il collegamento e' via mare. Il
    secondo: la **localita'** e' un'isola, e allora qualunque ultimo miglio
    stimato su strada e' impossibile a prescindere dal tipo di fermata da cui
    parte. Senza questo controllo il motore proponeva un treno fino a Marsala
    e poi quaranta minuti di autobus fino a Favignana."""
    if is_island(place):
        # Non si confrontano gli identificatori: la gamba porta il nodo
        # dell'operatore (`alb:<uuid>`), la localita' il nostro
        # (`ov:port-favignana`). Sono lo stesso molo con due nomi, e l'unico
        # confronto che regge e' la distanza.
        return any(
            node_distance_km(candidate, node) <= SAME_PORT_KM
            for candidate in place.nodes
        )
    if node.kind is not NodeKind.PORT:
        return True
    return transfers.reachable_by_land(_city_node(place, "probe"), node)


def transfer_between(prev: Leg, nxt: Leg) -> Leg | None:
    """Gamba di trasferimento fra due fermate diverse nella stessa tappa."""
    if prev.destination.id == nxt.origin.id:
        return None
    if node_distance_km(prev.destination, nxt.origin) <= SAME_NODE_KM:
        return None

    if not transfers.reachable_by_land(prev.destination, nxt.origin):
        return None
    estimate = transfers.estimate(prev.destination, nxt.origin)
    depart = prev.arrive
    return Leg(
        provider="transfer",
        mode=Mode.TRANSFER,
        origin=prev.destination,
        destination=nxt.origin,
        depart=depart,
        arrive=depart + timedelta(minutes=estimate.duration_min),
        operator="trasferimento",
        fare=None,
        notes=[estimate.description],
    )


def _city_node(place: Place, suffix: str) -> Node:
    return Node(
        id=f"city:{place.label.lower()}:{suffix}",
        name=place.label,
        kind=NodeKind.CITY,
        lat=place.lat,
        lon=place.lon,
        country=place.country,
        city=place.label,
        timezone=place.nodes[0].timezone if place.nodes else None,
    )


def first_mile(place: Place, first: Leg) -> Leg | None:
    """Come si arriva alla fermata di partenza, se non e' gia' dove sei.

    Conta nel tempo totale: un volo da Malpensa preso partendo da Torino comincia
    due ore prima del decollo, e ignorarlo falsa ogni confronto."""
    start = _city_node(place, "start")
    if node_distance_km(start, first.origin) <= 2.5:
        return None

    estimate = transfers.estimate(start, first.origin)
    buffer_min = 15 if first.mode is not Mode.AIR else 90
    arrive = first.depart - timedelta(minutes=buffer_min)
    return Leg(
        provider="transfer",
        mode=Mode.TRANSFER,
        origin=start,
        destination=first.origin,
        depart=arrive - timedelta(minutes=estimate.duration_min),
        arrive=arrive,
        operator="avvicinamento",
        fare=None,
        notes=[estimate.description],
    )


def last_mile(place: Place, last: Leg) -> Leg | None:
    """Come si arriva davvero a destinazione dall'ultima fermata servita.

    E' la voce che ribalta i confronti: atterrare a Bari non significa essere a
    Matera, ci sono ancora settantacinque minuti di pullman."""
    end = _city_node(place, "end")
    if node_distance_km(last.destination, end) <= 2.5:
        return None

    estimate = transfers.estimate(last.destination, end, destination_label=place.label)
    depart = last.arrive + timedelta(minutes=10 if last.mode is not Mode.AIR else 35)
    return Leg(
        provider="transfer",
        mode=Mode.TRANSFER,
        origin=last.destination,
        destination=end,
        depart=depart,
        arrive=depart + timedelta(minutes=estimate.duration_min),
        operator="ultimo miglio",
        fare=None,
        notes=[estimate.description],
    )
