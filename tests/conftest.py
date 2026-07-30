"""Costruttori di gambe e nodi sintetici per i test del motore.

Il motore di composizione e' la logica di valore del progetto e deve essere
verificabile senza rete: qui si fabbricano gambe con orari e prezzi decisi da
noi, cosi' i test dicono cosa fa il motore e non cosa faceva Trenitalia quel
giorno.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models import Fare, Leg, Mode, Node, NodeKind, Place, SearchQuery


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item, nextitem) -> None:
    """Chiude la connessione a SQLite aperta dal test appena finito.

    Serve perche' aiosqlite tiene la connessione su un thread interno non
    demone: finche' resta aperta il processo non muore, e la suite sembra
    bloccata pur avendo passato tutti i test. Il test successivo avra' un event
    loop nuovo e riaprira' da solo (`db.get_db` lo prevede), quindi chiudere qui
    non toglie niente a nessuno.

    Va fatto **prima** dei teardown dei fixture, finche' l'event loop del test
    e' ancora vivo: chiudere una connessione dal loop sbagliato non da' errore,
    si blocca."""
    from app.orchestrator import db

    connection, loop = db._connection, db._loop
    if connection is None:
        return
    if loop is None or loop.is_closed():
        db._connection = None
        db._loop = None
        return
    try:
        loop.run_until_complete(db.close_db())
    except RuntimeError:
        # Loop gia' in esecuzione o in chiusura: meglio lasciare la connessione
        # al prossimo giro che far fallire un test per una pulizia.
        pass

ROME = ZoneInfo("Europe/Rome")
DAY = date(2026, 8, 14)


def make_node(
    node_id: str,
    name: str,
    lat: float,
    lon: float,
    kind: NodeKind = NodeKind.STATION,
    *,
    iata: str | None = None,
    city: str | None = None,
    providers: dict[str, str] | None = None,
) -> Node:
    return Node(
        id=node_id,
        name=name,
        kind=kind,
        lat=lat,
        lon=lon,
        country="IT",
        city=city,
        timezone="Europe/Rome",
        iata=iata,
        provider_ids=providers or {},
    )


def at(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=ROME) + timedelta(
        days=day_offset
    )


def make_leg(
    provider: str,
    mode: Mode,
    origin: Node,
    destination: Node,
    depart: datetime,
    arrive: datetime,
    price: float | None = None,
    **kwargs,
) -> Leg:
    return Leg(
        provider=provider,
        mode=mode,
        origin=origin,
        destination=destination,
        depart=depart,
        arrive=arrive,
        operator=kwargs.pop("operator", provider),
        fare=Fare(amount=price) if price is not None else None,
        **kwargs,
    )


# --- Geografia dei casi di prova: il corridoio Torino-Matera ---------------

TORINO_PN = make_node("t:pn", "Torino Porta Nuova", 45.061, 7.678, providers={"x": "1"})
TORINO_AIR = make_node(
    "air:TRN", "Torino Caselle", 45.200, 7.649, NodeKind.AIRPORT, iata="TRN", city="Torino"
)
BARI_C = make_node("t:bari", "Bari Centrale", 41.118, 16.871, city="Bari", providers={"x": "2"})
BARI_AIR = make_node(
    "air:BRI", "Bari Palese", 41.139, 16.766, NodeKind.AIRPORT, iata="BRI", city="Bari"
)
MATERA_BUS = make_node(
    "b:matera", "Matera autostazione", 40.671, 16.589, NodeKind.BUS_STOP, city="Matera"
)


@pytest.fixture
def torino() -> Place:
    return Place(
        query="Torino",
        label="Torino",
        lat=45.0703,
        lon=7.6869,
        country="IT",
        nodes=[TORINO_PN, TORINO_AIR],
    )


@pytest.fixture
def matera() -> Place:
    return Place(
        query="Matera",
        label="Matera",
        lat=40.6663,
        lon=16.6044,
        country="IT",
        nodes=[MATERA_BUS],
    )


@pytest.fixture
def query() -> SearchQuery:
    return SearchQuery(origin="Torino", destination="Matera", date=DAY)
