"""Le contromisure che rendono sostenibili venticinque adapter.

Con tre operatori qualunque strategia funziona. Il rischio compare quando gli
adapter diventano molti: se ognuno vedesse tutti i percorsi, una ricerca
genererebbe migliaia di richieste e decine di sessioni browser, e non finirebbe
mai. Questi test bloccano proprio quella regressione.
"""

from __future__ import annotations

import pytest

from app.models import Mode, Node, NodeKind, SearchQuery
from app.providers import coverage
from app.routing import composer

from .conftest import BARI_C, MATERA_BUS, TORINO_PN


def _path(*labels: str) -> composer.CandidatePath:
    points = [
        composer.Waypoint(label, label.title(), 45.0 + i, 7.0 + i, (TORINO_PN,))
        for i, label in enumerate(labels)
    ]
    return composer.CandidatePath(points)


def test_gli_adapter_lenti_vedono_meno_percorsi() -> None:
    """Un adapter che apre un browser non puo' girare su tutti i percorsi:
    dieci adapter per tredici tratte sarebbero centotrenta sessioni."""
    paths = [
        _path("origin", "dest"),
        _path("origin", "hub:Bari", "dest"),
        _path("origin", "hub:Roma", "dest"),
        _path("origin", "hub:Napoli", "dest"),
        _path("origin", "hub:Bari", "hub:Taranto", "dest"),
    ]

    veloci = composer.paths_for_tier(paths, tier=1)
    lenti = composer.paths_for_tier(paths, tier=2)

    assert len(veloci) == len(paths)
    assert len(lenti) < len(veloci)
    # I percorsi a due scali sono riservati ai veloci: costano il doppio delle
    # tratte e producono le soluzioni meno probabili.
    assert all(len(p.waypoints) <= 3 for p in lenti)
    # Il diretto non deve mai sparire.
    assert lenti[0].waypoints[0].key == "origin"
    assert len(lenti[0].waypoints) == 2


def test_i_compiti_non_si_duplicano_fra_percorsi() -> None:
    """La stessa tratta compare in piu' percorsi: va interrogata una volta sola."""
    query = SearchQuery(origin="Torino", destination="Matera", date=__import__("datetime").date(2026, 8, 14))
    paths = [_path("origin", "hub:Bari", "dest"), _path("origin", "hub:Bari", "hub:X", "dest")]
    tasks = composer.tasks_for_tier(paths, query, tier=1)
    keys = [provider.route_key(o, d) for _, provider, o, d in tasks]
    assert len(keys) == len(set(keys))


# ----------------------------------------------------------------- copertura


@pytest.mark.asyncio
async def test_non_sapere_non_e_un_rifiuto() -> None:
    """Distinzione che decide se un operatore sparisce dalle ricerche.

    Se l'elenco rotte non arriva, `serves` deve dire "non lo so" e la ricerca
    deve provarci lo stesso. Trattare l'assenza di informazione come un no
    spegnerebbe l'operatore in silenzio, senza nessun errore visibile."""
    await coverage.forget("provaX")
    assert await coverage.serves("provaX", "AAA", "BBB") is None

    await coverage.remember("provaX", "AAA", ["BBB", "CCC"])
    assert await coverage.serves("provaX", "AAA", "BBB") is True
    assert await coverage.serves("provaX", "AAA", "ZZZ") is False
    # Le chiavi sono normalizzate: un operatore che scrive minuscolo non deve
    # far fallire il confronto.
    assert await coverage.serves("provaX", "aaa", "bbb") is True
    await coverage.forget("provaX")


@pytest.mark.asyncio
async def test_caricatore_fallito_lascia_lo_stato_ignoto() -> None:
    await coverage.forget("provaY")

    async def rotto():
        raise RuntimeError("endpoint irraggiungibile")

    assert await coverage.ensure("provaY", "AAA", rotto) is None
    # Non deve nemmeno memorizzare un insieme vuoto, che equivarrebbe a un no.
    assert await coverage.serves("provaY", "AAA", "BBB") is None


@pytest.mark.asyncio
async def test_caricatore_riuscito_viene_chiamato_una_volta_sola() -> None:
    await coverage.forget("provaZ")
    chiamate = {"n": 0}

    async def loader():
        chiamate["n"] += 1
        return ["BBB", "ccc"]

    assert await coverage.ensure("provaZ", "AAA", loader) == ["BBB", "CCC"]
    assert await coverage.ensure("provaZ", "AAA", loader) == ["BBB", "CCC"]
    assert chiamate["n"] == 1
    await coverage.forget("provaZ")


# ------------------------------------------------------------ nodi utilizzabili


def test_un_operatore_vede_solo_le_fermate_che_sa_identificare() -> None:
    """Il filtro che aveva silenziosamente escluso tutti i voli: le compagnie
    aeree identificano gli scali con lo IATA, non con un ID di dataset."""
    from app.providers import registry

    ryanair = registry.get("ryanair")
    aeroporto = Node(
        id="air:TRN", name="Torino Caselle", kind=NodeKind.AIRPORT,
        lat=45.2, lon=7.6, iata="TRN",
    )
    stazione = Node(
        id="t:pn", name="Torino Porta Nuova", kind=NodeKind.STATION,
        lat=45.06, lon=7.68, provider_ids={"trenitalia": "8300219"},
    )
    assert ryanair.supports_node(aeroporto)
    assert not ryanair.supports_node(stazione)

    trenitalia = registry.get("trenitalia")
    assert trenitalia.supports_node(stazione)
    assert not trenitalia.supports_node(
        Node(id="x", name="Fermata ignota", kind=NodeKind.STATION, lat=0, lon=0)
    )


def test_ogni_adapter_dichiara_una_tratta_di_prova() -> None:
    """Senza `sample_route` il comando di salute non puo' accorgersi che un
    parser si e' rotto, ed e' l'unico modo per scoprirlo prima dell'utente."""
    from app.providers import registry

    senza = [p.id for p in registry.all_providers() if not p.sample_route]
    assert not senza, f"adapter senza sample_route: {senza}"
