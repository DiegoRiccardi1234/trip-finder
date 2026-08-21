"""I parser degli adapter, verificati su risposte reali congelate.

Questi test non toccano la rete. Servono a intercettare la rottura piu' comune
del progetto: l'operatore cambia la forma della risposta e il parser comincia a
restituire zero gambe, o gambe sbagliate, senza sollevare eccezioni.

Per aggiungere o aggiornare una fixture:

    python scripts/try_provider.py flixbus Torino Matera 2026-08-14 --save
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import orjson
import pytest

from app.config import FIXTURES_DIR
from app.models import Leg, Mode, Node
from app.providers import registry
from app.providers.base import NotServed, ProviderError, SearchContext
from app.providers.http_client import HttpError
from app.providers.rail import trenitalia
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


# ------------------------------------------- tariffe ridotte di Trenitalia


def test_una_tariffa_ridotta_non_entra_nel_prezzo_esposto() -> None:
    """`minPrice` esclude di proposito le offerte legate a una tessera.

    E' il motivo per cui il prezzo esposto non e' il piu' basso in assoluto: chi
    cerca non ha necessariamente la CartaFRECCIA, e il motore non lo sa."""
    from app.providers.rail import trenitalia

    grid = {
        "services": [
            {
                "minPrice": {"amount": 37.90},
                "offers": [
                    {"status": "SALEABLE", "name": "Super Economy", "price": {"amount": 37.90}},
                    {"status": "SALEABLE", "name": "FrecciaYOUNG", "price": {"amount": 29.00}},
                ],
            }
        ]
    }
    assert trenitalia._conditional_total({"grids": [grid]}, 37.90) == (29.00, {"FrecciaYOUNG"})


def test_senza_offerte_condizionate_non_si_promette_niente() -> None:
    """Se sotto il minimo non c'e' nulla, non c'e' niente da dire: una riga in
    piu' su ogni treno sarebbe rumore, e il rumore si smette di leggere."""
    from app.providers.rail import trenitalia

    grid = {
        "services": [
            {
                "minPrice": {"amount": 37.90},
                "offers": [
                    {"status": "SALEABLE", "name": "Super Economy", "price": {"amount": 37.90}},
                    {"status": "SALEABLE", "name": "BASE", "price": {"amount": 61.00}},
                ],
            }
        ]
    }
    assert trenitalia._conditional_total({"grids": [grid]}, 37.90) is None


def test_un_esaurito_non_diventa_una_tariffa_ridotta() -> None:
    """La stessa regola del prezzo SOLD_OUT: quello che non si puo' comprare non
    e' un'opzione, e prometterlo sarebbe peggio che tacerlo."""
    from app.providers.rail import trenitalia

    grid = {
        "services": [
            {
                "minPrice": {"amount": 37.90},
                "offers": [
                    {"status": "SALEABLE", "name": "Super Economy", "price": {"amount": 37.90}},
                    {"status": "SOLD_OUT", "name": "FrecciaYOUNG", "price": {"amount": 29.00}},
                ],
            }
        ]
    }
    assert trenitalia._conditional_total({"grids": [grid]}, 37.90) is None


@pytest.mark.skipif(
    not (FIXTURES_DIR / "trenitalia" / "torino-bari.json").exists(),
    reason="fixture Trenitalia assente",
)
def test_le_tariffe_ridotte_arrivano_gia_nella_risposta_vera() -> None:
    """Sulla risposta reale congelata: le offerte con tessera ci sono da sempre,
    e fino a oggi venivano buttate insieme al resto della griglia."""
    payload = orjson.loads((FIXTURES_DIR / "trenitalia" / "torino-bari.json").read_bytes())
    provider = registry.get("trenitalia")
    ctx = SearchContext(date=date.fromisoformat(payload["captured_for"]["date"]))

    legs = provider.parse(
        payload["raw"],
        Node(**payload["origin_node"]),
        Node(**payload["destination_node"]),
        ctx,
    )

    ridotte = [nota for leg in legs for nota in leg.notes if "invece di" in nota]
    assert ridotte, "nessuna tariffa ridotta riconosciuta su una risposta che ne contiene"
    assert any("Freccia" in nota or "YOUNG" in nota for nota in ridotte)


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


@needs_datasets
def test_gli_operatori_del_mondo_si_dichiarano() -> None:
    """Fuori Europa la ricerca non trova niente, ed e' onesto: quello che non e'
    onesto e' tacere che il collegamento esista. Shinkansen, Amtrak e gli altri
    non si possono interrogare, ma si possono nominare — con il link, come si fa
    da sempre con Italo e Tirrenia."""
    from app.geo.resolver import get_resolver
    from app.providers import known_routes

    resolver = get_resolver()
    tutti = {Mode.RAIL, Mode.BUS, Mode.AIR, Mode.FERRY}

    def nomi(origine: str, destinazione: str) -> set[str]:
        suggeriti = known_routes.suggestions(
            resolver.resolve(origine), resolver.resolve(destinazione), tutti
        )
        for voce in suggeriti:
            assert voce["url"].startswith("https://"), f"{voce['name']} senza link"
            assert voce["note"], f"{voce['name']} senza spiegazione"
        return {voce["name"] for voce in suggeriti}

    assert "Shinkansen (JR Central)" in nomi("Tokyo", "Osaka")
    assert "Amtrak" in nomi("New York", "Washington")
    assert "Eurostar" in nomi("Londra", "Parigi")
    # E dove invece cerchiamo davvero non si suggerisce nessuno al posto nostro.
    assert not nomi("Torino", "Matera")


# ------------------------------------ Trenitalia: da che ora guarda la giornata


class _RispostaFinta:
    """Il minimo che `Trenitalia.fetch` legge di una risposta HTTP."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _HttpFinto:
    """Registra cosa chiediamo a Trenitalia e con quale contemporaneita'.

    Serve a leggere **cosa chiediamo**: e' li' che stava il difetto, non nel
    parsing. Le pagine si indirizzano per `offset` e non per ordine di arrivo,
    perche' da quando partono insieme l'ordine non e' piu' garantito.

    `max_in_volo` e' il modo deterministico di misurare il parallelismo: contare
    i secondi renderebbe il test una misura della velocita' della macchina."""

    def __init__(
        self,
        pagine: list[dict] | None = None,
        rompe_a: set[int] | None = None,
        lente: set[int] | None = None,
    ) -> None:
        self.pagine = pagine or [{"solutions": []}]
        self.rompe_a = rompe_a or set()
        self.lente = lente or set()
        self.inviati: list[dict] = []
        self.in_volo = 0
        self.max_in_volo = 0

    async def post(self, url: str, **kwargs) -> _RispostaFinta:
        payload = kwargs.get("json") or {}
        self.inviati.append(payload)
        offset = (payload.get("criteria") or {}).get("offset", 0)
        self.in_volo += 1
        self.max_in_volo = max(self.max_in_volo, self.in_volo)
        try:
            # Cede il controllo: senza un punto di sospensione le coroutine
            # finirebbero una per volta e il parallelismo non si vedrebbe.
            await asyncio.sleep(30 if offset in self.lente else 0)
            if offset in self.rompe_a:
                raise HttpError(f"finto guasto sull'offset {offset}")
            indice = offset // trenitalia.PAGINA
            if indice >= len(self.pagine):
                return _RispostaFinta({"solutions": []})
            return _RispostaFinta(self.pagine[indice])
        finally:
            self.in_volo -= 1

    def offsets(self) -> list[int]:
        return sorted((p.get("criteria") or {}).get("offset", 0) for p in self.inviati)


def _stazione(node_id: str, nome: str, trenitalia_id: str, lat: float, lon: float) -> Node:
    return Node(
        id=node_id,
        name=nome,
        kind="station",
        lat=lat,
        lon=lon,
        country="IT",
        timezone="Europe/Rome",
        provider_ids={"trenitalia": trenitalia_id},
    )


FIRENZE_SMN = _stazione("tl:8434", "Firenze Santa Maria Novella", "8306421", 43.7768, 11.2478)
TORINO_PN_RAIL = _stazione("tl:8300219", "Torino Porta Nuova", "8300219", 45.0625, 7.6785)

#: La tratta che l'utente ha chiesto. Solo su questa si spende la giornata
#: intera: sulle coincidenze intermedie la prima pagina e' quello che c'e'.
CHIESTA = (frozenset({FIRENZE_SMN.id}), frozenset({TORINO_PN_RAIL.id}))


async def test_l_ora_minima_di_partenza_arriva_fino_a_trenitalia() -> None:
    """Chi ha un impegno la mattina cerca il ritorno del pomeriggio, e il
    pomeriggio non arrivava mai.

    Misurato il 2026-08-17 su Firenze SMN -> Torino Porta Nuova: il backend
    restituisce **dieci** soluzioni per richiesta e ignora il `limit`, quindi
    partire dalla mezzanotte copre fino alle 07:53 e basta. Ancorando invece la
    richiesta alle 13:30 risponde 13:37 -> 16:55. L'ora che l'utente ha gia'
    dichiarato e' l'informazione che rende la richiesta utile: va usata."""
    provider = registry.get("trenitalia")
    http = _HttpFinto()
    ctx = SearchContext(date=date(2026, 8, 27), http=http, depart_after=time(13, 30))

    await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    assert http.inviati, "nessuna richiesta inviata"
    assert http.inviati[0]["departureTime"] == "2026-08-27T13:30:00.000"


async def test_senza_ora_minima_si_parte_dalla_mezzanotte() -> None:
    """Il comportamento di prima resta quello giusto quando non c'e' un vincolo:
    chi non ha dichiarato un'ora vuole vedere la giornata dall'inizio."""
    provider = registry.get("trenitalia")
    http = _HttpFinto()
    ctx = SearchContext(date=date(2026, 8, 27), http=http)

    await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    assert http.inviati[0]["departureTime"] == "2026-08-27T00:01:00.000"


def _soluzioni(quante: int, prima_ora: int = 6) -> dict:
    return {
        "solutions": [
            {"solution": {"departureTime": f"2026-08-27T{prima_ora + i:02d}:00:00.000"}}
            for i in range(quante)
        ]
    }


async def test_la_giornata_si_chiede_a_pagine() -> None:
    """Il backend risponde dieci soluzioni per volta e non si puo' alzare la
    pagina: `pageSize`, `size`, `maxResults` e `numberOfSolutions` sono chiavi
    sconosciute (HTTP 400), `limit` viene accettato e ignorato. L'unico modo di
    vedere la giornata e' `offset`, che cammina contiguo — misurato il
    2026-08-17 su Firenze SMN -> Torino Porta Nuova: 00:40, 07:55, 12:28, 14:55,
    19:55."""
    provider = registry.get("trenitalia")
    http = _HttpFinto([_soluzioni(10), _soluzioni(10), _soluzioni(3, prima_ora=16)])
    ctx = SearchContext(date=date(2026, 8, 27), http=http, endpoints=CHIESTA)

    raw = await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    assert http.offsets() == [0, 10, 20, 30, 40]
    assert len(raw["solutions"]) == 23


async def test_le_pagine_dopo_la_prima_partono_insieme() -> None:
    """In fila indiana la giornata costava **11,94 s** (cinque pagine da ~2,4 s,
    misurate il 2026-08-17), e il budget di un provider `tier 1` e' 18 s: sotto
    carico si sfora, e tre timeout aprono il circuito per cinque minuti. Il
    risultato era peggiore del difetto che la paginazione doveva curare — prima
    si vedevano solo i treni del mattino, poi nessun treno.

    Gli `offset` non dipendono l'uno dall'altro, quindi non c'e' motivo di
    aspettarli in fila."""
    provider = registry.get("trenitalia")
    http = _HttpFinto([_soluzioni(10)] * 5)
    ctx = SearchContext(date=date(2026, 8, 27), http=http, endpoints=CHIESTA)

    await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    assert http.max_in_volo > 1, "le pagine sono ancora in fila indiana"


async def test_una_coincidenza_intermedia_si_ferma_alla_prima_pagina() -> None:
    """Venti coppie di stazioni per ricerca, cinque pagine a testa, sono cento
    richieste allo stesso host: ventinove secondi di coda contro i diciotto di
    budget, misurati il 2026-08-17 su Prato -> Torino. Le ultime sforavano e
    l'operatore veniva dichiarato guasto — cioe' i treni sparivano del tutto.

    La giornata intera si paga dove serve: sulla tratta cercata. Per una gamba
    di mezzo la prima pagina e' quello che c'e'."""
    provider = registry.get("trenitalia")
    http = _HttpFinto([_soluzioni(10)] * 5)
    ctx = SearchContext(date=date(2026, 8, 27), http=http)  # nessun endpoint: e' di mezzo

    await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    assert len(http.inviati) == 1


async def test_la_prima_pagina_resta_una_sonda() -> None:
    """La tratta magra deve costare quanto costava: una richiesta sola.

    E' il motivo per cui la prima pagina si aspetta da sola invece di partire
    insieme alle altre: su una tratta con pochi treni chiedere il resto sarebbe
    lavoro buttato su ogni coppia di stazioni di ogni ricerca."""
    provider = registry.get("trenitalia")
    http = _HttpFinto([_soluzioni(4)])
    ctx = SearchContext(date=date(2026, 8, 27), http=http)

    await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    assert len(http.inviati) == 1


async def test_una_pagina_persa_non_porta_via_la_giornata() -> None:
    """Restituire il mattino e' meglio che restituire niente.

    Le pagine dopo la prima sono un di piu': se una non arriva, quello che e'
    arrivato vale comunque. Sollevare l'eccezione farebbe fallire l'intero
    provider — e un provider che fallisce ripetutamente si spegne da solo."""
    provider = registry.get("trenitalia")
    http = _HttpFinto([_soluzioni(10)] * 5, rompe_a={20})
    ctx = SearchContext(date=date(2026, 8, 27), http=http, endpoints=CHIESTA)

    raw = await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    assert len(raw["solutions"]) == 40, "la pagina persa doveva togliere solo se stessa"


async def test_le_pagine_in_piu_hanno_un_tempo_massimo(monkeypatch) -> None:
    """Nel caso peggiore si vedono meno corse, mai zero.

    Una ricerca tocca decine di coppie di stazioni e tutte chiedono allo stesso
    host: se la coda si allunga, aspettare comunque tutte le pagine porta la
    chiamata oltre i 18 s di budget, e tre volte di fila il circuito si apre —
    cioe' Trenitalia sparisce del tutto dalla classifica. Le pagine dopo la
    prima sono un di piu': hanno un tempo, e chi non arriva viene lasciato.

    E' la differenza fra «qualche corsa in meno» e «nessun treno»."""
    monkeypatch.setattr(trenitalia, "BUDGET_PAGINE", 0.05)
    provider = registry.get("trenitalia")
    http = _HttpFinto([_soluzioni(10)] * 5, lente={20, 30, 40})
    ctx = SearchContext(date=date(2026, 8, 27), http=http, endpoints=CHIESTA)

    raw = await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)

    # La prima e la seconda ce l'hanno fatta, le tre lente no.
    assert len(raw["solutions"]) == 20


async def test_se_manca_la_prima_pagina_si_fallisce() -> None:
    """La prima invece e' la ricerca: senza, non c'e' niente da salvare, e dire
    «nessun treno» quando non lo sappiamo sarebbe una risposta inventata."""
    provider = registry.get("trenitalia")
    http = _HttpFinto([_soluzioni(10)], rompe_a={0})
    ctx = SearchContext(date=date(2026, 8, 27), http=http)

    with pytest.raises(ProviderError):
        await provider.fetch(FIRENZE_SMN, TORINO_PN_RAIL, ctx)


def test_due_ore_di_partenza_diverse_non_condividono_la_cache() -> None:
    """La chiave di cache dice quale risposta e' riusabile. Da quando l'ora
    minima cambia **la richiesta**, due ricerche sulla stessa tratta e data non
    ricevono piu' la stessa risposta: senza questo, la ricerca col vincolo si
    riprenderebbe le corse del mattino gia' in cache e il difetto tornerebbe
    identico, ma invisibile perche' senza traffico di rete."""
    provider = registry.get("trenitalia")
    giorno = date(2026, 8, 27)

    mezzanotte = provider.cache_key(
        FIRENZE_SMN, TORINO_PN_RAIL, SearchContext(date=giorno)
    )
    pomeriggio = provider.cache_key(
        FIRENZE_SMN, TORINO_PN_RAIL, SearchContext(date=giorno, depart_after=time(13, 30))
    )

    assert mezzanotte != pomeriggio


# ------------------------------------- la gamba che non risponde alla domanda


def _nodo(nid: str, nome: str, lat: float, lon: float) -> Node:
    return Node(id=nid, name=nome, kind="station", lat=lat, lon=lon, country="IT")


ASTI = _nodo("tl:20216", "Asti", 44.9009, 8.2065)
CANELLI = _nodo("tl:21031", "Canelli", 44.7192, 8.2871)


def test_una_gamba_di_un_altra_regione_non_entra_in_classifica() -> None:
    """Il difetto del 2026-08-22, bloccato dove passano tutti gli adapter.

    Una ricerca Asti-Canelli ha ricevuto da FlixBus la corsa Castiglione della
    Pescaia -> Lamezia Terme: diciotto ore per ventun chilometri. Entrava in
    classifica perche' portava **le coordinate della tratta chiesta** con i
    nomi di un'altra, e ogni controllo a valle guardava le coordinate."""
    from app.providers.base import perche_non_risponde

    finta = Leg(
        provider="flixbus",
        mode=Mode.BUS,
        origin=_nodo("flix:a", "Castiglione della Pescaia", ASTI.lat, ASTI.lon),
        destination=_nodo("flix:b", "Lamezia Terme", CANELLI.lat, CANELLI.lon),
        depart=datetime(2026, 8, 23, 18, 18, tzinfo=ZoneInfo("Europe/Rome")),
        arrive=datetime(2026, 8, 24, 12, 29, tzinfo=ZoneInfo("Europe/Rome")),
    )

    motivo = perche_non_risponde(finta, ASTI, CANELLI)
    assert motivo, "una corsa di diciotto ore per ventun chilometri deve essere scartata"
    assert "plausibile" in motivo


def test_una_gamba_vera_e_corta_non_viene_scartata() -> None:
    """Il rovescio: il controllo non deve mangiarsi il trasporto locale.

    Asti-Canelli in autobus sono cinquantadue minuti (linea 41, misurata). Una
    soglia sulla sola velocita' media, senza l'attesa concessa, scarterebbe le
    gambe corte, che sono esattamente quelle che mancano."""
    from app.providers.base import perche_non_risponde

    vera = Leg(
        provider="transitous",
        mode=Mode.BUS,
        origin=ASTI,
        destination=CANELLI,
        depart=datetime(2026, 8, 24, 10, 2, tzinfo=ZoneInfo("Europe/Rome")),
        arrive=datetime(2026, 8, 24, 10, 54, tzinfo=ZoneInfo("Europe/Rome")),
    )

    assert perche_non_risponde(vera, ASTI, CANELLI) == ""


@pytest.mark.skipif(not FIXTURES, reason="nessuna fixture salvata")
@pytest.mark.parametrize("provider_id,path", CASI)
def test_nessuna_gamba_delle_fixture_viene_scartata(provider_id: str, path) -> None:
    """La rete di sicurezza deve prendere il difetto e nient'altro.

    Parametrizzato come `test_parser_su_fixture`, e non con un ciclo interno,
    perche' cosi' FlixBus entra davvero: e' l'adapter che ha prodotto il
    difetto, ed e' anche l'unico che ha bisogno dei dataset geografici. Un
    ciclo che lo saltasse per comodita' proverebbe tutto tranne il caso che
    conta.

    Misurato sulle 150 gambe salvate: la piu' lenta e' un regionale svizzero
    che usa il 27% del tempo concesso. Se questo test diventa rosso, la soglia
    sta mangiando un adapter sano e va guardata prima di alzarla."""
    from app.providers.base import perche_non_risponde

    payload = orjson.loads(path.read_bytes())
    origin = Node(**payload["origin_node"])
    destination = Node(**payload["destination_node"])
    captured = payload["captured_for"]
    ctx = SearchContext(
        date=date.fromisoformat(captured["date"]), pax=captured.get("pax", 1)
    )

    scartate = [
        motivo
        for leg in registry.get(provider_id).parse(payload["raw"], origin, destination, ctx)
        if (motivo := perche_non_risponde(leg, origin, destination))
    ]
    assert not scartate, f"gambe sane scartate dal controllo: {scartate}"


# ---------------------------------- FlixBus non indovina piu' le localita'


def test_l_autocomplete_fuzzy_non_puo_piu_spedirci_in_un_altra_regione() -> None:
    """I due casi veri, misurati sull'API il 2026-08-22.

    `q=asti` propone "Castiglione della Pescaia" (punteggio 17,95) e
    `q=acqui terme` propone "Lamezia Terme" (22,17). Il confronto e' per parole
    intere e non per sottostringa proprio per il primo: `castiglione` contiene
    `asti`, e un controllo per sottostringa lo avrebbe accettato."""
    from app.providers.bus.flixbus import _stesso_posto

    assert not _stesso_posto("asti", "Castiglione della Pescaia")
    assert not _stesso_posto("acqui terme", "Lamezia Terme")
    assert not _stesso_posto("canelli", "Cannelli di Sopra")


def test_le_localita_giuste_restano_riconosciute() -> None:
    """Il confronto vale nelle due direzioni perche' i cataloghi si troncano a
    vicenda: nessuno dei due nomi e' quello ufficiale dell'altro."""
    from app.providers.bus.flixbus import _stesso_posto

    assert _stesso_posto("asti", "Asti")
    assert _stesso_posto("milano", "Milano")
    assert _stesso_posto("forli", "Forlì")
    assert _stesso_posto("bolzano", "Bolzano/Bozen")
    assert _stesso_posto("san marzano oliveto", "San Marzano")
    assert _stesso_posto("reggio emilia", "Reggio nell'Emilia")


def test_la_citta_si_riconosce_dalle_coordinate_non_dal_nome() -> None:
    """I numeri veri dell'autocomplete, misurati il 2026-08-22.

    Quando FlixBus indovina la citta' sta a 0-3 km da quella chiesta; quando
    sbaglia sta a centinaia. Non c'e' zona grigia, ed e' il motivo per cui il
    criterio e' geografico: sui nomi i due cataloghi non vanno d'accordo
    ("Parigi" contro "Paris") e nessuna soglia di somiglianza separa i buoni
    dai cattivi."""
    from app.providers.bus.flixbus import _e_qui

    asti = _nodo("tl:20216", "Asti", 44.9009, 8.2065)
    castiglione = {"id": "x", "name": "Castiglione della Pescaia",
                   "location": {"lat": 42.7639, "lon": 10.8750}}
    assert not _e_qui(castiglione, asti, "asti"), "320 km non sono Asti"

    canelli = _nodo("tl:21031", "Canelli", 44.7192, 8.2871)
    lamezia = {"id": "y", "name": "Lamezia Terme",
               "location": {"lat": 38.9629, "lon": 16.3093}}
    assert not _e_qui(lamezia, canelli, "acqui terme"), "922 km non sono l'astigiano"

    milano = _nodo("tl:8490", "Milano Centrale", 45.4871, 9.2048)
    vera = {"id": "z", "name": "Milano", "location": {"lat": 45.4881, "lon": 9.1997}}
    assert _e_qui(vera, milano, "milano")


def test_senza_coordinate_si_ripiega_sul_nome() -> None:
    """Il ripiego serve al giorno in cui l'autocomplete cambia forma: meglio un
    criterio debole di nessun criterio."""
    from app.providers.bus.flixbus import _e_qui

    asti = _nodo("tl:20216", "Asti", 44.9009, 8.2065)
    assert _e_qui({"id": "a", "name": "Asti"}, asti, "asti")
    assert not _e_qui({"id": "b", "name": "Castiglione della Pescaia"}, asti, "asti")
    assert not _e_qui({"name": "Asti"}, asti, "asti"), "senza id non e' utilizzabile"


@needs_datasets
def test_la_fermata_di_genova_non_diventa_antibes() -> None:
    """L'unico aggancio che l'indice produceva era sbagliato.

    Misurato il 2026-08-22: delle 27 fermate nelle fixture, `flixbus_id` ne
    aggancia due, ed entrambe sono «Genova (Fanti d'Italia/Principe)», che
    l'indice risolve in **Antibes** (Francia, 170 km da Torino). Il ramo
    "conosciuto" era piu' pericoloso di quello di ripiego, perche' sembrava
    autorevole."""
    from app.providers.bus.flixbus import _station_node

    torino = _nodo("tl:8567", "Torino Porta Nuova", 45.0610, 7.6777)
    stazioni = {"g": {"legacy_id": 3938, "name": "Genova (Fanti d'Italia/Principe)"}}

    nodo = _station_node("g", stazioni, torino)
    assert "Antibes" not in nodo.name
    assert nodo.country != "FR"


# --------------------------------------------- Transitous: il trasporto locale


def test_transitous_legge_gli_orari_nel_fuso_della_fermata() -> None:
    """MOTIS manda UTC con la `Z` e il fuso della fermata a parte.

    Letti come locali, ogni corsa risulterebbe due ore prima: e' la stessa
    trappola gia' pagata con Albatross, dove sbagliavano insieme il
    riconoscimento del viaggio notturno e la penalita' sull'ora di arrivo."""
    from app.providers.bus import transitous

    istante = transitous._momento("2026-08-24T08:12:00Z", "Europe/Rome")
    assert istante is not None
    assert istante.hour == 10, "le 08:12 UTC sono le 10:12 a Roma"
    assert istante.utcoffset().total_seconds() == 2 * 3600


def test_transitous_non_conta_la_camminata_dentro_la_gamba() -> None:
    """La gamba comincia alla fermata, non sul marciapiede di casa.

    Il primo e l'ultimo miglio li calcola `routing/feasibility.py` col suo
    modello uniforme: sommarne due diversi sarebbe peggio che sceglierne uno.
    La camminata resta scritta nei segmenti, dove si legge senza entrare nel
    conto."""
    provider = registry.get("transitous")
    ctx = SearchContext(date=date(2026, 8, 24))
    grezzo = {
        "itineraries": [
            {
                "transfers": 0,
                "legs": [
                    {"mode": "WALK", "duration": 600,
                     "from": {"name": "START", "lat": 44.9009, "lon": 8.2065, "tz": "Europe/Rome"},
                     "to": {"name": "ASTI - AUTOSTAZIONE", "lat": 44.896053, "lon": 8.208715,
                            "tz": "Europe/Rome"}},
                    {"mode": "BUS", "routeShortName": "41", "agencyName": "Coas",
                     "startTime": "2026-08-24T08:12:00Z", "endTime": "2026-08-24T08:52:00Z",
                     "from": {"name": "ASTI - AUTOSTAZIONE", "stopId": "a",
                              "lat": 44.896053, "lon": 8.208715, "tz": "Europe/Rome"},
                     "to": {"name": "CANELLI - PAESE", "stopId": "b",
                            "lat": 44.718586, "lon": 8.287022, "tz": "Europe/Rome"}},
                    {"mode": "WALK", "duration": 120,
                     "from": {"name": "CANELLI - PAESE", "lat": 44.718586, "lon": 8.287022,
                              "tz": "Europe/Rome"},
                     "to": {"name": "END", "lat": 44.7192, "lon": 8.2871, "tz": "Europe/Rome"}},
                ],
            }
        ]
    }

    (leg,) = provider.parse(grezzo, ASTI, CANELLI, ctx)
    assert leg.duration_min == 40, "40 minuti di autobus, non i 52 con la camminata"
    assert leg.origin.name == "ASTI - AUTOSTAZIONE"
    assert any("a piedi" in s for s in leg.segments)
    assert leg.fare is None, "non pubblica prezzi, e non deve inventarne"


def test_transitous_non_spaccia_un_treno_per_un_pullman() -> None:
    """Si dichiara pullman e deve restituire pullman.

    Il ranker non rifiltra per modo: un treno consegnato sotto la bandiera del
    bus scavalcherebbe in silenzio la scelta di chi ha tolto la spunta al
    treno."""
    provider = registry.get("transitous")
    ctx = SearchContext(date=date(2026, 8, 24))
    grezzo = {
        "itineraries": [
            {
                "transfers": 0,
                "legs": [
                    {"mode": "RAIL", "routeShortName": "R", "agencyName": "x",
                     "startTime": "2026-08-24T08:12:00Z", "endTime": "2026-08-24T08:52:00Z",
                     "from": {"name": "A", "stopId": "a", "lat": 44.9, "lon": 8.2,
                              "tz": "Europe/Rome"},
                     "to": {"name": "B", "stopId": "b", "lat": 44.7, "lon": 8.3,
                            "tz": "Europe/Rome"}},
                ],
            }
        ]
    }

    assert provider.parse(grezzo, ASTI, CANELLI, ctx) == []


@pytest.mark.asyncio
async def test_transitous_tace_fuori_dalla_tratta_cercata() -> None:
    """La disciplina di richiesta si prova contando le chiamate, non i risultati.

    Dietro c'e' un servizio di volontari che chiede di non fare molte
    richieste: sulle coincidenze intermedie questo adapter non deve nemmeno
    aprire la connessione."""
    provider = registry.get("transitous")

    class _MaiChiamato:
        async def get_json(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("non doveva chiedere niente")

    ctx = SearchContext(
        date=date(2026, 8, 24),
        endpoints=(frozenset({"tl:altro"}), frozenset({"tl:ancora-altro"})),
        http=_MaiChiamato(),
    )
    with pytest.raises(NotServed):
        await provider.fetch(ASTI, CANELLI, ctx)


def test_transitous_tace_sulla_lunga_percorrenza() -> None:
    """Torino -> Matera risponde 200 con zero itinerari: verificato il
    2026-08-22. Chiedere comunque vorrebbe dire spendere il calcolo piu' caro
    che hanno per ricevere una lista vuota."""
    provider = registry.get("transitous")
    torino = _nodo("tl:8567", "Torino Porta Nuova", 45.0610, 7.6777)
    matera = _nodo("ov:matera", "Matera Centrale", 40.6670, 16.6063)

    assert not provider.can_serve(torino, matera)
    assert provider.can_serve(ASTI, CANELLI)


def test_transitous_dichiara_chi_e() -> None:
    """La politica d'uso chiede nome, versione e un contatto. E' un obbligo, e
    un obbligo che si puo' verificare va verificato."""
    from app.providers.bus.transitous import USER_AGENT

    assert USER_AGENT.startswith("TripFinder/")
    assert "github.com" in USER_AGENT


def test_un_viaggio_assurdo_ma_vero_non_viene_scartato() -> None:
    """Il falso positivo che ha fatto ritarare la soglia, il 2026-08-22.

    Trenitalia vende davvero Bari Centrale -> Matera come Frecciarossa 8302 +
    9583 + FrecciaLink: 07:40 -> 19:20, cioe' cinquantacinque chilometri in
    linea d'aria e settecento minuti, perche' sale a nord e ritorna. E' un
    biglietto vero e acquistabile, e con la prima soglia — tarata solo sulle
    fixture che avevo in mano — spariva senza che nessuno lo sapesse.

    Questo test esiste per non rifare quell'errore: chi in futuro vorra'
    stringere il controllo deve prima spiegare cosa fa di questo viaggio."""
    from app.providers.base import perche_non_risponde

    bari = _nodo("tl:19401", "Bari Centrale", 41.117702, 16.870172)
    matera = _nodo("tl:21644", "Matera", 40.666000, 16.604000)
    lungo = Leg(
        provider="trenitalia",
        mode=Mode.RAIL,
        origin=bari,
        destination=matera,
        depart=datetime(2026, 8, 28, 7, 40, tzinfo=ZoneInfo("Europe/Rome")),
        arrive=datetime(2026, 8, 28, 19, 20, tzinfo=ZoneInfo("Europe/Rome")),
    )

    assert perche_non_risponde(lungo, bari, matera) == ""


def test_una_gamba_che_arriva_in_un_altra_regione_viene_scartata() -> None:
    """L'altro mezzo del controllo, e non e' teorico: durante una prova a
    schermo del 2026-08-22 l'adapter InterSAJ ha restituito una corsa Torino ->
    **Sibari** dentro una ricerca Torino -> Bari Centrale. Arrivava a 156 km
    dalla meta' chiesta, e prima sarebbe entrata in classifica."""
    from app.providers.base import perche_non_risponde

    torino = _nodo("tl:8567", "Torino Porta Nuova", 45.0610, 7.6777)
    bari = _nodo("tl:19401", "Bari Centrale", 41.117702, 16.870172)
    sibari = _nodo("x", "Sibari", 39.7560, 16.4500)
    sbagliata = Leg(
        provider="intersaj",
        mode=Mode.BUS,
        origin=torino,
        destination=sibari,
        depart=datetime(2026, 8, 28, 20, 0, tzinfo=ZoneInfo("Europe/Rome")),
        arrive=datetime(2026, 8, 29, 8, 0, tzinfo=ZoneInfo("Europe/Rome")),
    )

    motivo = perche_non_risponde(sbagliata, torino, bari)
    assert motivo
    assert "km da Bari Centrale" in motivo
