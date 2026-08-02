"""La pagina, guidata da un browser vero.

Duemilaseicento righe di JavaScript che nessun test aveva mai toccato. Il
motore e' coperto bene, ma la parte che l'utente guarda — la numerazione delle
schede, i riferimenti dentro il consiglio, l'ordinamento, il ciclo di vita del
blocco che chiede all'IA — era verificata solo a mano, aprendo il sito e
guardando. Le cose che si rompono li' non danno errore: danno una schermata
diversa.

Due scelte che tengono in piedi tutto questo:

  - **Si serve solo `app/static/`**, con un server statico da dieci righe.
    Alzare l'applicazione vera vorrebbe dire costruire l'indice geografico, che
    vuole trentasette megabyte di dataset che la CI non scarica: i test
    fallirebbero li' e solo li'. La pagina non ne ha bisogno — quello che
    verifichiamo e' il suo comportamento, non le ricerche.
  - **I dati si iniettano**, non si cercano. Una ricerca vera dipende da una
    dozzina di operatori esterni e dalla loro giornata; qui le soluzioni sono
    finte e decise da noi, cosi' un test rosso significa che il codice e'
    cambiato, non che FlixBus era lento.
"""

from __future__ import annotations

import http.server
import socket
import threading
from functools import partial
from pathlib import Path

import pytest

#: Si serve `app/`, non `app/static/`: la pagina chiama i suoi file come
#: `/static/app.js`, che e' il percorso con cui li serve l'applicazione vera.
#: Servendo direttamente la cartella dei file statici quei percorsi non
#: esisterebbero, la pagina si caricherebbe **senza JavaScript** e i test
#: fallirebbero tutti con «non e' definito» invece che per un motivo vero.
APP_DIR = Path(__file__).parent.parent / "app"


def _porta_libera() -> int:
    with socket.socket() as presa:
        presa.bind(("127.0.0.1", 0))
        return presa.getsockname()[1]


@pytest.fixture(scope="module")
def sito():
    """Serve `app/static/` su una porta qualsiasi, per la durata del modulo."""
    porta = _porta_libera()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(APP_DIR))
    # `SimpleHTTPRequestHandler` scrive una riga di log per richiesta: qui e'
    # rumore che finisce nell'output dei test.
    handler.log_message = lambda *args, **kwargs: None  # type: ignore[method-assign]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", porta), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{porta}/static/index.html"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def pagina(sito):
    """Una pagina caricata, con le soluzioni finte gia' dentro.

    Il browser si salta se non e' installato: `patchright install chromium` non
    e' scontato su una macchina qualsiasi, e un test che non puo' girare deve
    dirlo, non fallire."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("patchright non installato")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"browser non disponibile: {exc}")
        page = browser.new_page()
        page.goto(sito)
        yield page
        browser.close()


#: Un pannello con soluzioni finte, montato come lo monterebbe una ricerca.
#: `SearchPanel` vuole un `combo` e delle opzioni; le soluzioni arrivano dritte
#: in `trips`, saltando lo stream, che e' esattamente il pezzo che qui non
#: interessa.
PREPARA = """
(quante) => {
  document.getElementById('results').replaceChildren();
  panels.length = 0;
  // L'ordinamento e' una preferenza che sopravvive alla ricerca — voluto, e
  // qui va azzerato o un test si porta dietro la scelta di quello prima.
  lastSort = 'score';
  const panel = new SearchPanel(
    { origin: { value: 'Torino', field: 'origin' },
      destination: { value: 'Matera', field: 'destination' },
      date: '2026-08-07' },
    { roundtrip: false, returnDate: null, showDate: false },
  );
  panels.push(panel);
  const base = new Date('2026-08-07T06:00:00');
  panel.trips = Array.from({ length: quante }, (_, i) => {
    const partenza = new Date(base.getTime() + i * 3600e3);
    const arrivo = new Date(partenza.getTime() + (4 + i) * 3600e3);
    const itinerary = {
      id: `x${i}`,
      legs: [{ provider: 'finto', mode: 'bus', operator: `Operatore ${i}`,
               depart: partenza.toISOString(), arrive: arrivo.toISOString(),
               origin: { name: 'Torino' }, destination: { name: 'Matera' },
               is_transfer: false, internal_changes: 0 }],
      cost: { total: 100 - i * 7, currency: 'EUR', lines: [], has_estimates: false },
      depart: partenza.toISOString(), arrive: arrivo.toISOString(),
      duration_min: (4 + i) * 60, n_changes: 0, n_tickets: 1,
      modes: ['bus'], operators: [`Operatore ${i}`], flags: [],
      score: 10 - i, score_parts: {}, is_new: false,
    };
    return oneWayTrip(itinerary);
  });
  panel.numera();
  panel.render();
  return panel.trips.length;
}
"""


def _monta(pagina, quante: int = 5) -> None:
    _nel_mondo_della_pagina(pagina, PREPARA, quante)


def _nel_mondo_della_pagina(pagina, codice: str, argomento=None):
    """Esegue codice dove vive `app.js`.

    `patchright` e' un fork anti-rilevamento di Playwright e valuta il codice in
    un **mondo isolato**: vede il DOM, che e' condiviso, ma non le variabili
    della pagina — `panels` e `SearchPanel` risultano semplicemente non
    definite. Le letture del DOM restano isolate, che va benissimo; solo chi
    tocca lo stato dell'applicazione chiede il mondo vero."""
    return pagina.evaluate(codice, argomento, isolated_context=False)


def test_ogni_scheda_porta_il_suo_numero(pagina) -> None:
    """Il numero e' il nome con cui il consiglio chiama una soluzione: senza,
    «prenderei la [3]» non vuol dire niente."""
    _monta(pagina, 5)

    ref = pagina.eval_on_selector_all(
        ".itinerary[data-ref]", "nodi => nodi.map(n => n.dataset.ref)"
    )
    assert ref == ["1", "2", "3", "4", "5"]

    # E si vede davvero: l'ingombro renderizzato, non l'attributo che dovrebbe
    # averlo prodotto.
    box = pagina.eval_on_selector(
        ".itinerary .ref", "n => { const r = n.getBoundingClientRect(); return [r.width, r.height]; }"
    )
    assert box[0] > 0 and box[1] > 0


def test_il_numero_resta_attaccato_alla_soluzione(pagina) -> None:
    """La scelta di fondo: riordinando per prezzo i numeri **non** si
    rinumerano. Un consiglio gia' scritto continua a puntare alla soluzione
    giusta — se invece fossero le posizioni, punterebbe a un'altra scheda."""
    _monta(pagina, 5)
    prima = pagina.eval_on_selector_all(
        ".itinerary[data-ref]",
        "nodi => nodi.map(n => [n.dataset.ref, n.querySelector('.total').textContent])",
    )

    pagina.select_option(".sort-by", "price")
    dopo = pagina.eval_on_selector_all(
        ".itinerary[data-ref]",
        "nodi => nodi.map(n => [n.dataset.ref, n.querySelector('.total').textContent])",
    )

    assert [r for r, _ in dopo] != [r for r, _ in prima], "l'ordine non e' cambiato"
    assert dict(dopo) == dict(prima), "un numero indica una soluzione diversa da prima"


def test_il_riferimento_nel_consiglio_porta_alla_scheda(pagina) -> None:
    """Il pezzo che rende il consiglio verificabile: si legge «la [3]», si
    clicca, e si ha davanti quella scheda."""
    _monta(pagina, 5)
    _nel_mondo_della_pagina(
        pagina, "() => mostraConsiglio(panels[0].adviceEl, 'Consiglio', 'Prenderei la [3].', '')"
    )

    chip = pagina.query_selector(".panel .advice .ref-chip[data-goto='3']")
    assert chip is not None, "il riferimento non e' diventato un bottone"

    chip.click()
    evidenziata = pagina.eval_on_selector(
        "[data-ref='3']", "n => n.classList.contains('puntata')"
    )
    assert evidenziata


def test_un_riferimento_inesistente_resta_testo(pagina) -> None:
    """Meglio un numero muto di un bottone che non porta da nessuna parte."""
    _monta(pagina, 3)
    _nel_mondo_della_pagina(
        pagina, "() => mostraConsiglio(panels[0].adviceEl, 'Consiglio', 'Prenderei la [9].', '')"
    )

    assert pagina.query_selector(".panel .advice .ref-chip[data-goto='9']") is None
    assert "[9]" in pagina.eval_on_selector(".panel .advice", "n => n.textContent")


def test_il_motivo_giusto_e_il_bottone_riprova(pagina) -> None:
    """Sul troncamento la pagina diceva «le chiavi si mettono nelle
    impostazioni», che era il rimedio di un altro problema, e non offriva
    nessun modo di riprovare."""
    _monta(pagina, 3)
    _nel_mondo_della_pagina(
        pagina,
        "() => mostraConsiglio(panels[0].adviceEl, 'Consiglio', null, 'truncated', () => {})",
    )

    testo = pagina.eval_on_selector(".panel .advice", "n => n.textContent")
    assert "troncato" in testo
    assert "chiavi" not in testo
    assert pagina.query_selector(".panel .advice .riprova-ia") is not None


def test_gli_asterischi_del_modello_non_arrivano_a_schermo(pagina) -> None:
    """Il prompt vieta il grassetto, ma un modello puo' disobbedire: la pagina
    rende testo semplice, non markdown."""
    _monta(pagina, 3)
    _nel_mondo_della_pagina(
        pagina,
        "() => mostraConsiglio(panels[0].adviceEl, 'Consiglio', 'Prenderei la **prima**.', '')",
    )

    testo = pagina.eval_on_selector(".panel .advice", "n => n.textContent")
    assert "**" not in testo
    assert "prima" in testo


def test_il_filtro_non_rinumera_le_schede(pagina) -> None:
    """Stessa regola dell'ordinamento: filtrare nasconde delle schede, non
    ribattezza quelle che restano."""
    _monta(pagina, 5)
    pagina.select_option(".mode-filter", "rail")
    rimaste = pagina.eval_on_selector_all(
        ".itinerary[data-ref]", "nodi => nodi.map(n => n.dataset.ref)"
    )
    assert rimaste == [], "le soluzioni finte sono in pullman, non in treno"

    pagina.select_option(".mode-filter", "")
    tornate = pagina.eval_on_selector_all(
        ".itinerary[data-ref]", "nodi => nodi.map(n => n.dataset.ref)"
    )
    assert tornate == ["1", "2", "3", "4", "5"]
