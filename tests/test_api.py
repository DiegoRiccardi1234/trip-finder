"""Gli endpoint dell'IA, chiamati come li chiama la pagina.

Il resto della suite prova le funzioni direttamente, e va benissimo: e' li' che
sta la logica. Ma fra la pagina e `advisor` c'e' un pezzo che nessuno provava —
la validazione del corpo, la forma della risposta, il degrado quando qualcosa
esplode — e proprio su quello si regge il comportamento a schermo: la pagina
decide se scrivere il consiglio, tacere o mostrare «Riprova» guardando due
campi, `text` e `reason`. Se cambiano nome, o se un errore diventa un 500
invece di un motivo, il blocco a schermo non lo dice: sparisce.

Vale identico per `/api/parse`, che ha un contratto in piu' e piu' fragile: una
domanda esce con **200**, non con un errore, perche' non e' un fallimento ma un
turno. Se diventasse un 4xx la pagina la mostrerebbe in rosso e chiuderebbe la
conversazione invece di continuarla — e nessun test della logica se ne
accorgerebbe, perche' li' l'eccezione e' giusta.

Nessuna rete: il modello non viene mai chiamato, `advise`/`compare`/`parse`
sono sostituiti dove serve.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from starlette.testclient import TestClient

from app.ai import client as ai_client
from app.models import TripPlan, TripStage


@pytest.fixture
def api():
    """Il client **senza** `with`, quindi senza avvio dell'applicazione.

    Non e' una scorciatoia: l'avvio costruisce l'indice geografico, che vuole i
    due dataset da 29 MB. In CI non ci sono — e con `with` questi test non si
    saltavano, esplodevano con `FileNotFoundError`. Gli endpoint del consiglio
    non toccano ne' l'indice ne' la rete: quello che serve provare qui e' il
    contratto fra la pagina e l'API."""
    from app.main import app

    return TestClient(app)


def _corpo(**extra) -> dict:
    """Il corpo che manda davvero la pagina (`chiediConsiglio` in `app.js`)."""
    base = {
        "label": "Torino → Matera",
        "origin": "Torino",
        "destination": "Matera",
        "date": date(2026, 8, 7).isoformat(),
        "return_date": None,
        "depart_after": None,
        "arrive_by": "21:00",
        "allow_night": True,
        "pax": 1,
        "with_checked_bag": False,
        "max_budget": 120.0,
        "raw_text": "devo essere a Matera venerdi' sera",
        "found": 25,
        "partial": False,
        "relaxed": [{"kind": "max_budget", "value": 120.0}],
        "options": [
            {
                "ref": "1",
                "depart": datetime(2026, 8, 7, 18, 30).isoformat(),
                "arrive": datetime(2026, 8, 8, 8, 45).isoformat(),
                "duration_min": 855,
                "total": 80.0,
                "modes": ["bus"],
                "operators": ["Marino Autolinee"],
                "n_changes": 0,
                "n_tickets": 1,
                "flags": ["separate_tickets"],
                "notes": ["prezzo indicativo: 'a partire da'"],
                "return_depart": None,
                "return_arrive": None,
                "return_duration_min": None,
            },
            {
                "ref": "2",
                "depart": datetime(2026, 8, 7, 22, 0).isoformat(),
                "arrive": datetime(2026, 8, 8, 18, 25).isoformat(),
                "duration_min": 1225,
                "total": 24.98,
                "modes": ["bus"],
                "operators": ["Itabus"],
                "n_changes": 1,
                "n_tickets": 1,
                "flags": [],
                "notes": [],
                "return_depart": None,
                "return_arrive": None,
                "return_duration_min": None,
            },
        ],
    }
    return {**base, **extra}


def test_il_corpo_che_manda_la_pagina_e_accettato(api, monkeypatch) -> None:
    """Il contratto fra `app.js` e l'endpoint. Un campo rinominato da una parte
    sola qui diventa un 422, e a schermo un consiglio che non arriva mai."""

    async def finto(request):
        # E arriva intero: se `options` o `relaxed` cadessero nella validazione,
        # il consiglio parlerebbe di una realta' diversa da quella a schermo.
        assert [option.ref for option in request.options] == ["1", "2"]
        assert request.relaxed[0].kind == "max_budget"
        assert request.arrive_by == "21:00"
        return ai_client.Answer(
            completion=ai_client.Completion(
                text="Prenderei la [2].", model="m", finish_reason="stop"
            )
        )

    monkeypatch.setattr("app.ai.advisor.advise", finto)
    risposta = api.post("/api/advice", json=_corpo())

    assert risposta.status_code == 200
    assert risposta.json() == {"text": "Prenderei la [2].", "reason": ""}


def test_senza_soluzioni_non_c_e_niente_da_dire(api, monkeypatch) -> None:
    """`nothing` non e' un guasto dell'IA e la pagina non lo scrive: e' l'unico
    motivo che resta muto, e deve arrivare cosi' com'e'."""
    monkeypatch.setattr(ai_client, "is_configured", lambda: True)
    risposta = api.post("/api/advice", json=_corpo(options=[]))

    assert risposta.status_code == 200
    assert risposta.json() == {"text": None, "reason": "nothing"}


def test_un_guasto_diventa_un_motivo_non_un_cinquecento(api, monkeypatch) -> None:
    """Un consiglio mancato non deve rompere la pagina. Con un 500 il `fetch`
    del frontend cadrebbe nel ramo generico e direbbe «nessun modello ha
    risposto» senza che nessuno abbia mai risposto niente."""

    async def esplode(request):
        raise RuntimeError("il fornitore ha chiuso la connessione")

    monkeypatch.setattr("app.ai.advisor.advise", esplode)
    risposta = api.post("/api/advice", json=_corpo())

    assert risposta.status_code == 200
    assert risposta.json() == {"text": None, "reason": "failed"}


def test_un_corpo_senza_meta_viene_rifiutato(api) -> None:
    """La destinazione non ha un valore di ripiego: senza, il prompt direbbe
    «Viaggio: Torino -> None»."""
    corpo = _corpo()
    del corpo["destination"]

    assert api.post("/api/advice", json=corpo).status_code == 422


def test_il_confronto_vuole_almeno_due_possibilita(api) -> None:
    """Con una sola non c'e' confronto da fare, e il modello inventerebbe un
    paragone con qualcosa che non gli e' stato dato."""
    candidato = {
        "label": "Torino → Matera",
        "origin": "Torino",
        "destination": "Matera",
        "date": date(2026, 8, 7).isoformat(),
        "options": _corpo()["options"],
    }
    risposta = api.post("/api/advice/compare", json={"candidates": [candidato]})

    assert risposta.status_code == 422


# ------------------------------------------- «Interpreta», che a volte chiede


def _piano() -> TripPlan:
    return TripPlan(
        stages=[TripStage(origin="Torino", destination="Matera", date=date(2026, 8, 28))],
        raw_text="da Torino a Matera. venerdi 28",
    )


def _parse_finto(esito, ricevuti: list | None = None):
    """Al posto di `nl_query.parse`: `esito` e' un piano, o l'eccezione da alzare."""

    async def parse(text, today=None, history=None):
        if ricevuti is not None:
            ricevuti.append((text, history))
        if isinstance(esito, Exception):
            raise esito
        return esito

    return parse


def test_una_domanda_e_una_risposta_valida_non_un_errore(api, monkeypatch) -> None:
    """Il contratto su cui si regge tutta la conversazione.

    La pagina guarda `q.domanda` **dopo** aver controllato `response.ok`: se la
    domanda arrivasse con un 4xx finirebbe nel ramo rosso, la conversazione si
    chiuderebbe e l'utente si vedrebbe un errore al posto di una domanda. Un
    test sulla logica non puo' accorgersene, perche' li' l'eccezione e' giusta:
    e' qui che si decide che quell'eccezione diventa un 200."""
    from app.ai.nl_query import ServeAltro

    monkeypatch.setattr("app.ai.nl_query.parse", _parse_finto(ServeAltro("Per quando?")))
    risposta = api.post("/api/parse", json={"text": "da Torino a Matera", "messages": []})

    assert risposta.status_code == 200
    assert risposta.json() == {"domanda": "Per quando?"}


def test_un_guasto_invece_resta_un_errore(api, monkeypatch) -> None:
    """L'altro lato del confine. Se l'IA e' spenta non c'e' risposta dell'utente
    che possa sbloccare la cosa: mandarla come domanda farebbe chiedere la
    stessa cosa all'infinito. E `detail` deve restare una **stringa**, perche'
    la pagina la scrive cosi' com'e'."""
    from app.ai.nl_query import ParseFailed

    monkeypatch.setattr(
        "app.ai.nl_query.parse", _parse_finto(ParseFailed("nessun modello ha risposto"))
    )
    risposta = api.post("/api/parse", json={"text": "da Torino a Matera", "messages": []})

    assert risposta.status_code == 422
    assert risposta.json()["detail"] == "nessun modello ha risposto"


def test_il_corpo_della_conversazione_arriva_intero(api, monkeypatch) -> None:
    """Il contratto fra `interpret()` e l'endpoint: la pagina manda la storia
    **senza** il turno corrente, che viaggia in `text` e viene riappeso di la'.
    Se i due si disallineassero, la frase dell'utente comparirebbe due volte o
    non comparirebbe affatto."""
    visti: list = []
    monkeypatch.setattr("app.ai.nl_query.parse", _parse_finto(_piano(), visti))

    risposta = api.post("/api/parse", json={
        "text": "venerdì 28",
        "messages": [
            {"role": "user", "content": "da Torino a Matera"},
            {"role": "assistant", "content": "Per quando?"},
        ],
    })

    assert risposta.status_code == 200
    assert risposta.json()["stages"][0]["destination"] == "Matera"
    (testo, storia), = visti
    assert testo == "venerdì 28"
    assert [turno["role"] for turno in storia] == ["user", "assistant"]
    assert storia[0]["content"] == "da Torino a Matera"


def test_senza_turni_non_si_passa_una_storia_vuota(api, monkeypatch) -> None:
    """`storia or None`, in `main.py`, e' la riga che conserva il comportamento
    di prima: chi non usa la conversazione deve vedere esattamente la funzione
    di sempre, non una lista vuota che la fa ragionare in un altro modo."""
    visti: list = []
    monkeypatch.setattr("app.ai.nl_query.parse", _parse_finto(_piano(), visti))

    api.post("/api/parse", json={"text": "da Torino a Matera venerdi 28", "messages": []})

    (_, storia), = visti
    assert storia is None


def test_una_conversazione_troppo_lunga_viene_rifiutata(api) -> None:
    """Il muro dietro il tetto della pagina, e la ragione per cui `motivo()`
    esiste in `app.js`: qui a rifiutare e' la validazione, non il nostro codice,
    e allora `detail` non e' una stringa ma una **lista di oggetti**. Scriverla
    com'e' metterebbe «[object Object]» a schermo."""
    troppi = [{"role": "user", "content": "e poi?"} for _ in range(11)]
    risposta = api.post("/api/parse", json={"text": "venerdì", "messages": troppi})

    assert risposta.status_code == 422
    assert isinstance(risposta.json()["detail"], list)


def test_un_testo_vuoto_non_arriva_al_modello(api, monkeypatch) -> None:
    """Una richiesta vuota si ferma prima della rete. Costa zero, e su un piano
    gratuito le chiamate sprecate sono quelle che poi mancano."""
    chiamate: list = []
    monkeypatch.setattr("app.ai.nl_query.parse", _parse_finto(_piano(), chiamate))

    assert api.post("/api/parse", json={"text": "", "messages": []}).status_code == 422
    assert chiamate == []
