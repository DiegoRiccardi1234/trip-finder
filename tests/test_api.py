"""Gli endpoint del consiglio, chiamati come li chiama la pagina.

Il resto della suite prova le funzioni direttamente, e va benissimo: e' li' che
sta la logica. Ma fra la pagina e `advisor` c'e' un pezzo che nessuno provava —
la validazione del corpo, la forma della risposta, il degrado quando qualcosa
esplode — e proprio su quello si regge il comportamento a schermo: la pagina
decide se scrivere il consiglio, tacere o mostrare «Riprova» guardando due
campi, `text` e `reason`. Se cambiano nome, o se un errore diventa un 500
invece di un motivo, il blocco a schermo non lo dice: sparisce.

Nessuna rete: il modello non viene mai chiamato, `advise`/`compare` sono
sostituiti dove serve.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from starlette.testclient import TestClient

from app.ai import client as ai_client


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
