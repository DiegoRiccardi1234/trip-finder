"""La diagnostica distingue un guasto da una prova senza evidenza sufficiente."""

from datetime import date, timedelta
import sys

import pytest

from scripts import _bootstrap

sys.modules.setdefault("_bootstrap", _bootstrap)
from scripts import check_providers as checks

from app.models import Mode, NodeKind
from app.providers.base import Provider, ProviderError, SearchContext
from app.providers.bus.albatross import AlbatrossProvider, _hhmm, _stop_node
from app.providers.http_client import Blocked
from tests.conftest import make_leg, make_node, at

ORIGIN = make_node("test-a", "Torino", 45.07, 7.69, NodeKind.BUS_STOP, city="Torino")
DESTINATION = make_node("test-b", "Milano", 45.47, 9.19, NodeKind.BUS_STOP, city="Milano")


class DiagnosticProvider(Provider):
    id = "test"
    name = "Test"
    mode = Mode.BUS
    sample_route = ("Torino", "Milano")
    prepared = False
    raw = []

    def supports_node(self, node):
        return True

    async def sample_nodes(self, ctx):
        return ORIGIN, DESTINATION

    async def prepare_parse(self, ctx):
        self.prepared = True

    async def fetch(self, origin, destination, ctx):
        assert self.prepared
        return self.raw

    def parse(self, raw, origin, destination, ctx):
        return []


def test_date_explicit_and_expired_pin():
    provider = DiagnosticProvider()
    requested = date.today() + timedelta(days=14)
    provider.sample_date = (date.today() + timedelta(days=7)).isoformat()
    assert checks._sample_day(provider, requested) == date.fromisoformat(provider.sample_date)
    assert checks._sample_day(provider, requested, explicit_date=True) == requested
    provider.sample_date = (date.today() - timedelta(days=1)).isoformat()
    assert checks._sample_day(provider, requested) == requested
    assert checks._pin_scaduto(provider)
    provider.sample_date = "invalid"
    assert checks._sample_day(provider, requested) == requested
    assert not checks._pin_scaduto(provider)


@pytest.mark.parametrize("tally,expected", [
    ({"ok": 2}, 0), ({"vuoto": 1}, 2), ({"bloccato": 1}, 2),
    ({"saltato": 1}, 2), ({"assente": 1}, 2),
    ({"errore": 1, "vuoto": 2}, 1),
])
def test_exit_codes(tally, expected):
    assert checks._exit_code(tally) == expected


@pytest.mark.parametrize("raw", [[], [{"canceled": True}], {"solutions": []}])
async def test_empty_raw_or_filtered_result_is_inconclusive(raw):
    provider = DiagnosticProvider()
    provider.raw = raw
    status, legs, _, detail = await checks.check_one(provider, date.today(), False, False)
    assert status == "vuoto" and legs == 0
    assert "non verificato" in detail
    assert provider.prepared


async def test_parser_failure_is_error():
    provider = DiagnosticProvider()
    def parse(*args):
        raise ProviderError("formato cambiato")
    provider.parse = parse
    assert (await checks.check_one(provider, date.today(), False, False))[0] == "errore"


async def test_wrapped_http_block_remains_inconclusive():
    provider = DiagnosticProvider()
    async def fetch(*args):
        try:
            raise Blocked("anti-bot")
        except Blocked as exc:
            raise ProviderError("richiesta fallita") from exc
    provider.fetch = fetch
    assert (await checks.check_one(provider, date.today(), False, False))[0] == "bloccato"


async def test_only_incoherent_legs_is_error():
    provider = DiagnosticProvider()
    far = make_node("far", "Roma", 41.90, 12.50, NodeKind.BUS_STOP, city="Roma")
    provider.parse = lambda *args: [make_leg("test", Mode.BUS, far, DESTINATION, at(8), at(10))]
    assert (await checks.check_one(provider, date.today(), False, False))[0] == "errore"


async def test_albatross_diagnostic_prepares_real_stop_catalog(monkeypatch):
    provider = AlbatrossProvider()
    provider.id, provider.name = "catalog-test", "Catalog test"
    provider.sample_route = ("Torino", "Milano")
    catalog = {"localities": {"torino": "Torino", "milano": "Milano"}, "stops": {
        "1": {"name": "Torino autostazione", "locality": "Torino", "lat": 45.07, "lon": 7.69},
        "2": {"name": "Milano Lampugnano", "locality": "Milano", "lat": 45.49, "lon": 9.13},
    }}
    async def get_catalog(ctx):
        return catalog
    async def fetch(origin, destination, ctx):
        return [{"trips": [{"departureStopId": "1", "arrivalStopId": "2",
            "departureDateTime": f"{ctx.date}T08:00:00+00:00",
            "arrivalDateTime": f"{ctx.date}T10:00:00+00:00"}], "calculatedPrice": 12}]
    monkeypatch.setattr(provider, "_catalog", get_catalog)
    monkeypatch.setattr(provider, "fetch", fetch)
    parsed = []
    original_parse = provider.parse
    def capture_parse(*args):
        legs = original_parse(*args)
        parsed.extend(legs)
        return legs
    monkeypatch.setattr(provider, "parse", capture_parse)
    assert (await checks.check_one(provider, date.today(), False, False))[0] == "ok"
    assert parsed[0].origin.name == "Torino autostazione"
    assert parsed[0].destination.name == "Milano Lampugnano"
    assert parsed[0].destination.lon == 9.13


async def test_search_prepares_parser_even_when_raw_is_cached(monkeypatch):
    from app.orchestrator import cache
    provider = DiagnosticProvider()
    async def get_cached(key):
        return []
    monkeypatch.setattr(cache, "get", get_cached)
    await provider.search(ORIGIN, DESTINATION, SearchContext(date=date.today()))
    assert provider.prepared


@pytest.mark.parametrize("stamp,expected", [
    ("2026-08-14T16:30:00+00:00", "18:30"),
    ("2026-10-26T16:30:00+00:00", "17:30"),
    ("2026-08-14T18:30:00", "18:30"),
    ("invalid", "?"),
])
def test_segment_times_use_the_same_local_timezone_as_legs(stamp, expected):
    assert _hhmm(stamp, "Europe/Rome") == expected


def test_albatross_ferry_stop_is_a_port():
    fallback = ORIGIN.model_copy(update={"kind": NodeKind.PORT})
    stops = {"1": {"name": "Porto", "lat": 45.07, "lon": 7.69}}
    assert _stop_node(stops, "1", fallback, Mode.FERRY).kind is NodeKind.PORT
    assert _stop_node(stops, "1", ORIGIN, Mode.BUS).kind is NodeKind.BUS_STOP
