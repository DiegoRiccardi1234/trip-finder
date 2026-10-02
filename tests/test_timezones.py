"""Durate reali e margini di coincidenza durante i cambi d'ora."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.models import Itinerary, Mode
from app.routing import feasibility

from .conftest import BARI_AIR, BARI_C, MATERA_BUS, TORINO_PN, make_leg

ROME = ZoneInfo("Europe/Rome")


@pytest.mark.parametrize(
    ("depart", "arrive", "minutes"),
    [
        (
            datetime(2026, 10, 25, 2, 45, tzinfo=ROME, fold=0),
            datetime(2026, 10, 25, 2, 15, tzinfo=ROME, fold=1),
            30,
        ),
        (
            datetime(2026, 3, 29, 1, 50, tzinfo=ROME),
            datetime(2026, 3, 29, 3, 10, tzinfo=ROME),
            20,
        ),
        (
            datetime(2026, 10, 2, 9, tzinfo=ROME),
            datetime(2026, 10, 2, 10, tzinfo=ROME),
            60,
        ),
    ],
)
def test_durate_leg_e_itinerario_sono_tempo_trascorso(depart, arrive, minutes):
    leg = make_leg("rail", Mode.RAIL, TORINO_PN, BARI_C, depart, arrive, 10.0)
    itinerary = Itinerary(id="dst", legs=[leg])

    assert leg.duration_min == minutes
    assert itinerary.duration_min == minutes
    assert leg.model_dump()["duration_min"] == minutes
    assert itinerary.model_dump()["duration_min"] == minutes


@pytest.mark.parametrize(
    ("arrive", "transfer_depart", "transfer_arrive", "next_depart", "gap", "margin"),
    [
        (
            datetime(2026, 10, 25, 2, 45, tzinfo=ROME, fold=0),
            datetime(2026, 10, 25, 2, 50, tzinfo=ROME, fold=0),
            datetime(2026, 10, 25, 2, 5, tzinfo=ROME, fold=1),
            datetime(2026, 10, 25, 2, 15, tzinfo=ROME, fold=1),
            30, 15,
        ),
        (
            datetime(2026, 3, 29, 1, 50, tzinfo=ROME),
            datetime(2026, 3, 29, 1, 55, tzinfo=ROME),
            datetime(2026, 3, 29, 3, 5, tzinfo=ROME),
            datetime(2026, 3, 29, 3, 10, tzinfo=ROME),
            20, 10,
        ),
    ],
)
def test_connessione_e_trasferimento_usano_istanti_reali(
    arrive, transfer_depart, transfer_arrive, next_depart, gap, margin,
):
    prev = make_leg(
        "rail", Mode.RAIL, TORINO_PN, BARI_C,
        arrive.replace(hour=0, minute=30, fold=0), arrive, 20.0,
    )
    nxt = make_leg(
        "air", Mode.AIR, BARI_AIR, MATERA_BUS,
        next_depart, next_depart.replace(hour=4, minute=0, fold=0), 30.0,
    )
    transfer = make_leg(
        "transfer", Mode.TRANSFER, BARI_C, BARI_AIR, transfer_depart, transfer_arrive,
    )

    assert Itinerary(id="without-transfer", legs=[prev, nxt]).min_connection_min == gap
    assert Itinerary(id="with-transfer", legs=[prev, transfer, nxt]).min_connection_min == margin


@pytest.mark.parametrize(
    ("arrive", "next_depart", "minutes"),
    [
        (
            datetime(2026, 10, 25, 2, 45, tzinfo=ROME, fold=0),
            datetime(2026, 10, 25, 2, 15, tzinfo=ROME, fold=1),
            30,
        ),
        (
            datetime(2026, 3, 29, 1, 50, tzinfo=ROME),
            datetime(2026, 3, 29, 3, 10, tzinfo=ROME),
            20,
        ),
        (
            datetime(2026, 10, 25, 2, 45, tzinfo=ROME, fold=0),
            datetime(2026, 10, 24, 21, 15, tzinfo=ZoneInfo("America/New_York")),
            30,
        ),
    ],
)
def test_coincidenza_dst_e_fusi_diversi_non_viene_scartata(arrive, next_depart, minutes):
    prev = make_leg(
        "rail", Mode.RAIL, TORINO_PN, BARI_C,
        arrive.replace(hour=0, minute=30, fold=0), arrive, 20.0,
    )
    nxt = make_leg(
        "rail", Mode.RAIL, BARI_C, MATERA_BUS,
        next_depart, next_depart.replace(hour=next_depart.hour + 1), 20.0,
    )

    assert feasibility.gap_minutes(prev, nxt) == minutes
    assert feasibility.required_margin(prev, nxt) == 10
    assert feasibility.connection_ok(prev, nxt)
