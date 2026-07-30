"""I parser degli adapter, verificati su risposte reali congelate.

Questi test non toccano la rete. Servono a intercettare la rottura piu' comune
del progetto: l'operatore cambia la forma della risposta e il parser comincia a
restituire zero gambe, o gambe sbagliate, senza sollevare eccezioni.

Per aggiungere o aggiornare una fixture:

    python scripts/try_provider.py flixbus Torino Matera 2026-08-14 --save
"""

from __future__ import annotations

from datetime import date

import orjson
import pytest

from app.config import FIXTURES_DIR
from app.models import Leg, Node
from app.providers import registry
from app.providers.base import SearchContext
from tests._datasets import needs_datasets

#: FlixBus non pubblica le coordinate delle sue fermate: il parser le recupera
#: dal dataset Trainline incrociando il `legacy_id`, quindi e' l'unico che senza
#: i dataset geografici non puo' girare. Tutti gli altri lavorano solo sulla
#: risposta salvata, che e' il motivo per cui esistono queste fixture.
PROVIDER_CON_DATASET = {"flixbus"}


def _fixtures() -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    if not FIXTURES_DIR.exists():
        return found
    for path in sorted(FIXTURES_DIR.glob("*/*.json")):
        found.append((path.parent.name, path))
    return found


FIXTURES = _fixtures()
CASI = [
    pytest.param(
        provider_id,
        path,
        marks=[needs_datasets] if provider_id in PROVIDER_CON_DATASET else [],
        id=f"{provider_id}/{path.stem}",
    )
    for provider_id, path in FIXTURES
]


@pytest.mark.skipif(not FIXTURES, reason="nessuna fixture salvata")
@pytest.mark.parametrize("provider_id,path", CASI)
def test_parser_su_fixture(provider_id: str, path) -> None:
    payload = orjson.loads(path.read_bytes())
    provider = registry.get(provider_id)

    origin = Node(**payload["origin_node"])
    destination = Node(**payload["destination_node"])
    captured = payload["captured_for"]
    ctx = SearchContext(
        date=date.fromisoformat(captured["date"]), pax=captured.get("pax", 1)
    )

    legs = provider.parse(payload["raw"], origin, destination, ctx)

    assert legs, "il parser non ha estratto nessuna gamba da una risposta valida"
    for leg in legs:
        _assert_leg_coerente(leg, provider_id)


def _assert_leg_coerente(leg: Leg, provider_id: str) -> None:
    assert leg.provider == provider_id
    assert leg.depart.tzinfo is not None, "orario di partenza senza fuso"
    assert leg.arrive.tzinfo is not None, "orario di arrivo senza fuso"
    assert leg.arrive > leg.depart, "arrivo prima della partenza"
    assert 0 < leg.duration_min < 60 * 48, f"durata implausibile: {leg.duration_min} min"
    if leg.fare is not None:
        assert leg.fare.amount > 0, "tariffa a zero o negativa"
        assert leg.fare.amount < 5000, "tariffa implausibile"
        assert leg.fare.currency == "EUR"
    assert leg.internal_changes >= 0
    assert leg.origin.id and leg.destination.id


@pytest.mark.skipif(not FIXTURES, reason="nessuna fixture salvata")
def test_ogni_adapter_con_fixture_e_registrato() -> None:
    for provider_id, _ in FIXTURES:
        assert registry.get(provider_id) is not None


def test_albatross_converte_gli_orari_utc_in_locali() -> None:
    """Il fuso della piattaforma Albatross, bloccato contro il sito reale.

    L'API serializza in UTC e allega il fuso a parte. Leggerla senza convertire
    sposta ogni corsa di due ore in estate: il notturno Torino-Matera del 14
    agosto risulterebbe in partenza alle 16:30 invece che alle 18:30, e con lui
    sbaglierebbero il riconoscimento del viaggio notturno e la penalita'
    sull'ora di arrivo. I valori qui sotto sono quelli letti su
    booking.marinobus.it."""
    path = FIXTURES_DIR / "marino" / "torino-matera.json"
    if not path.exists():
        pytest.skip("fixture Marino non salvata")

    payload = orjson.loads(path.read_bytes())
    provider = registry.get("marino")
    ctx = SearchContext(date=date.fromisoformat(payload["captured_for"]["date"]))
    legs = provider.parse(
        payload["raw"],
        Node(**payload["origin_node"]),
        Node(**payload["destination_node"]),
        ctx,
    )

    orari = {
        (leg.depart.strftime("%H:%M"), leg.arrive.strftime("%H:%M")): leg
        for leg in legs
    }
    assert ("18:30", "08:45") in orari, f"orari letti: {sorted(orari)}"
    notturno = orari[("18:30", "08:45")]
    assert notturno.fare and notturno.fare.amount == 83.0
    assert notturno.internal_changes == 0  # il sito lo chiama "corsa diretta"
    assert notturno.duration_min == 14 * 60 + 15

    diurno = orari[("06:30", "21:35")]
    assert diurno.fare and diurno.fare.amount == 75.0
    assert diurno.internal_changes == 1


def test_grimaldi_tiene_solo_il_giorno_chiesto_e_divide_il_prezzo() -> None:
    """Due trappole del preventivo Grimaldi, entrambe silenziose.

    La risposta contiene qualche giorno attorno a quello richiesto: prese per
    buone, quelle partenze finirebbero in un itinerario datato un altro giorno.
    E il prezzo e' del gruppo, non della persona: con due passeggeri raddoppia,
    e senza dividerlo il confronto con gli altri operatori sarebbe falsato."""
    path = FIXTURES_DIR / "grimaldi" / "napoli-palermo.json"
    if not path.exists():
        pytest.skip("fixture Grimaldi non salvata")

    payload = orjson.loads(path.read_bytes())
    provider = registry.get("grimaldi")
    origin = Node(**payload["origin_node"])
    destination = Node(**payload["destination_node"])
    giorno = date.fromisoformat(payload["captured_for"]["date"])
    raw = payload["raw"]

    assert raw.count("cal_day") > 1, "la fixture dovrebbe contenere piu' partenze"

    legs = provider.parse(raw, origin, destination, SearchContext(date=giorno))
    assert legs, "nessuna partenza estratta"
    assert {leg.depart.date() for leg in legs} == {giorno}

    intero = legs[0].fare.amount
    in_due = provider.parse(
        raw, origin, destination, SearchContext(date=giorno, pax=2)
    )[0].fare.amount
    assert in_due == round(intero / 2, 2)


def _itabus_legs() -> list[Leg]:
    path = FIXTURES_DIR / "itabus" / "milano-roma.json"
    if not path.exists():
        pytest.skip("fixture Itabus non salvata")
    payload = orjson.loads(path.read_bytes())
    provider = registry.get("itabus")
    ctx = SearchContext(date=date.fromisoformat(payload["captured_for"]["date"]))
    return provider.parse(
        payload["raw"],
        Node(**payload["origin_node"]),
        Node(**payload["destination_node"]),
        ctx,
    )


def test_itabus_prezza_il_viaggio_intero_non_la_tratta_piu_economica() -> None:
    """Su una corsa con cambio il prezzo esposto era quello di mezzo viaggio.

    Le tariffe Itabus sono annidate per operatore, famiglia e classe, e sotto
    la classe c'e' `items[].passenger_fares[]`, cioe' la **scomposizione per
    tratta** dello stesso totale. Cercando il minimo in tutto l'albero si
    finiva li' dentro: Milano-Roma via Bologna risultava 24,99 invece di 64,98
    (24,99 + 39,99). Caso reale che l'ha fatto scoprire: Torino-Catania a 19,99
    contro i 59,98 del sito.

    Non e' un errore innocuo. Il ranker penalizza un cambio di 0.04, quindi una
    corsa prezzata a meta' scavalca ogni diretta e si prende la prima riga."""
    con_cambio = [leg for leg in _itabus_legs() if leg.internal_changes]
    assert con_cambio, "la fixture dovrebbe contenere corse con cambio"

    prezzi = {leg.fare.amount for leg in con_cambio if leg.fare}
    assert prezzi == {79.98, 64.98}, f"prezzi letti: {sorted(prezzi)}"
    # I valori del bug: sono le singole tratte, non devono piu' comparire.
    assert not prezzi & {39.99, 24.99}

    # Una corsa con cambio si apre e mostra dove si cambia, invece di una riga
    # sola che la fa sembrare diretta.
    assert all(len(leg.segments) > 1 for leg in con_cambio)


def test_itabus_il_prezzo_esposto_e_il_totale_di_una_offerta() -> None:
    """La forma generale del bug, indipendente dai numeri della fixture.

    Sono due affermazioni sulla struttura del JSON, non sui suoi valori:

    1. dentro un'offerta (una classe di una famiglia), `price` e' **la somma**
       dei prezzi delle sue tratte in `items[].passenger_fares[]`. E' questo a
       rendere sbagliato scendere piu' in basso;
    2. il prezzo che l'adapter espone e' il totale piu' basso fra le offerte,
       e non un valore preso a un livello qualsiasi dell'albero.

    Se un domani Itabus rinomina `items` o sposta i prezzi di un livello,
    questo test lo dice; quello sopra, legato a due importi, potrebbe non
    accorgersene."""
    path = FIXTURES_DIR / "itabus" / "milano-roma.json"
    if not path.exists():
        pytest.skip("fixture Itabus non salvata")
    raw = orjson.loads(path.read_bytes())["raw"]

    def offerte(node: object) -> list[dict]:
        """I nodi che sono davvero un'offerta: un totale piu' le sue tratte."""
        trovate: list[dict] = []
        if isinstance(node, dict):
            if isinstance(node.get("price"), (int, float)) and isinstance(
                node.get("items"), list
            ):
                trovate.append(node)
            for child in node.values():
                trovate.extend(offerte(child))
        elif isinstance(node, list):
            for child in node:
                trovate.extend(offerte(child))
        return trovate

    totali_per_corsa: dict[str, float] = {}
    controllate = 0
    for route in raw["data"]["outbound"]["routes"]:
        if not isinstance(route, dict):
            continue
        for offerta in offerte(route.get("rates")):
            tratte = [
                fare["price"]
                for item in offerta["items"]
                for fare in item.get("passenger_fares") or []
                if isinstance(fare.get("price"), (int, float))
            ]
            assert tratte, "un'offerta senza tratte: la struttura e' cambiata"
            assert abs(offerta["price"] - sum(tratte)) < 0.01, (
                f"il totale {offerta['price']} non e' la somma delle tratte {tratte}"
            )
            controllate += 1
            partenza = route["departure_timestamp"]
            precedente = totali_per_corsa.get(partenza)
            totali_per_corsa[partenza] = (
                offerta["price"] if precedente is None else min(precedente, offerta["price"])
            )
    assert controllate > 10, "troppo poche offerte controllate: fixture sospetta"

    for leg in _itabus_legs():
        if leg.fare is None:
            continue
        atteso = totali_per_corsa.get(leg.depart.strftime("%Y-%m-%dT%H:%M:%S%z"))
        assert atteso is not None, "corsa senza offerte ma con un prezzo"
        assert leg.fare.amount == round(atteso, 2)


def test_itabus_lascia_intatte_le_dirette() -> None:
    """Sulle dirette il prezzo era gia' giusto: la correzione non deve spostarlo.

    Li' `items` ha un elemento solo, quindi il prezzo della tratta coincide col
    totale e il vecchio minimo cadeva per caso sul valore corretto."""
    dirette = [leg for leg in _itabus_legs() if not leg.internal_changes and leg.fare]
    assert dirette, "la fixture dovrebbe contenere corse dirette"
    prezzi = sorted({leg.fare.amount for leg in dirette})
    assert prezzi == [16.99, 19.99, 24.99, 29.99, 39.99], f"prezzi letti: {prezzi}"
    assert all(not leg.segments for leg in dirette)


def test_oebb_legge_gli_arrivi_dopo_la_mezzanotte() -> None:
    """HAFAS passa a otto cifre quando la corsa scavalca il giorno.

    `01082900` sono le 08:29 del giorno dopo. Letto come `HHMMSS` darebbe
    un'ora impossibile e il notturno Vienna-Venezia sparirebbe dai risultati."""
    from app.providers.rail.oebb import _moment

    giorno = date(2026, 8, 20)
    sera = _moment("213900", 120, giorno)
    mattina = _moment("01082900", 120, giorno)

    assert sera is not None and mattina is not None
    assert (sera.hour, sera.minute) == (21, 39)
    assert mattina.date() == date(2026, 8, 21)
    assert (mattina.hour, mattina.minute) == (8, 29)
    assert mattina > sera
    assert _moment("banana", 120, giorno) is None


def test_known_routes_dice_solo_chi_copre_davvero() -> None:
    """L'elenco degli operatori non interrogabili non deve diventare rumore:
    compare solo dove quella tratta e' documentata."""
    from app.models import Mode, Place
    from app.providers import known_routes

    def posto(nome: str) -> Place:
        return Place(query=nome, label=nome, lat=0.0, lon=0.0, country="IT")

    traghetti = {Mode.FERRY}
    assert [
        op["id"]
        for op in known_routes.suggestions(
            posto("Civitavecchia"), posto("Olbia"), traghetti
        )
    ] == ["tirrenia"]
    # La rotta vale nei due versi, e non vale per una coppia che nessuno serve.
    assert known_routes.suggestions(posto("Olbia"), posto("Civitavecchia"), traghetti)
    assert not known_routes.suggestions(posto("Bergamo"), posto("Matera"), traghetti)
    # Il modo chiesto filtra: chi cerca solo treni non vuole sentir parlare di navi.
    assert not known_routes.suggestions(
        posto("Civitavecchia"), posto("Olbia"), {Mode.RAIL}
    )


def test_trenitalia_scarta_le_soluzioni_esaurite() -> None:
    """Il prezzo di una soluzione esaurita e' quello che resta da vendere.

    Caso reale del 30 luglio 2026, Torino Porta Susa -> Ciampino: con il
    Frecciarossa 9583 pieno, Trenitalia dichiara `SOLD_OUT` e un prezzo di
    **1,50 euro**, cioe' il solo regionale finale. Le soluzioni acquistabili
    sulla stessa tratta costavano fra 105 e 141 euro. Preso per buono, quel
    numero vince ogni classifica e manda l'utente su un treno che non puo'
    comprare."""
    from app.models import NodeKind
    from app.providers.rail.trenitalia import Trenitalia

    torino = Node(id="t:susa", name="Torino Porta Susa", kind=NodeKind.STATION,
                  lat=45.07, lon=7.66, timezone="Europe/Rome")
    ciampino = Node(id="t:ciampino", name="Ciampino", kind=NodeKind.STATION,
                    lat=41.80, lon=12.60, timezone="Europe/Rome")

    def soluzione(status: str, amount: float) -> dict:
        return {
            "departureTime": "2026-07-30T08:10:00.000+02:00",
            "arrivalTime": "2026-07-30T13:09:00.000+02:00",
            "status": status,
            "price": {"amount": amount, "currency": "€", "hideAmount": False},
            "trains": [
                {"trainCategory": "Frecciarossa", "name": "9583"},
                {"trainCategory": "Regionale", "name": "20075"},
            ],
        }

    provider = Trenitalia()
    esaurita = provider._to_leg(soluzione("SOLD_OUT", 1.5), torino, ciampino)
    assert esaurita is None, "una soluzione esaurita non e' un'opzione di viaggio"

    venduta = provider._to_leg(soluzione("SALEABLE", 105.4), torino, ciampino)
    assert venduta is not None and venduta.fare is not None
    assert venduta.fare.amount == 105.4

    # Uno stato che non conosciamo non deve portarsi dietro un prezzo di cui non
    # sappiamo il significato: meglio farlo stimare, dichiarandolo.
    ignota = provider._to_leg(soluzione("BOOKABLE_ONLY_ON_SITE", 1.5), torino, ciampino)
    assert ignota is not None and ignota.fare is None
    assert any("verificare" in nota for nota in ignota.notes)


def test_uic_verso_id_lefrecce() -> None:
    """La conversione fra le due numerazioni di Trenitalia e' il punto piu'
    silenziosamente rompibile dell'adapter: un ID sbagliato non da' errore,
    da' i treni di un'altra citta'."""
    from app.providers.rail.trenitalia import uic_to_location_id

    assert uic_to_location_id("8300219") == "830000219"  # Torino Porta Nuova
    assert uic_to_location_id("8301700") == "830001700"  # Milano Centrale
    assert uic_to_location_id("8311119") == "830011119"  # Bari Centrale
    assert uic_to_location_id("8308409") == "830008409"  # Roma Termini
    assert uic_to_location_id("") is None
    assert uic_to_location_id("abc") is None
