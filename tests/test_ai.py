"""La logica deterministica dell'integrazione IA, senza chiamare nessun modello.

Quello che si verifica qui e' la parte che decide *quale* modello usare e come
interpretare quello che risponde. E' il punto in cui gli errori sono silenziosi:
un JSON troncato che sembra valido, o un modello morto tenuto in lista, non
danno errore, danno risultati sbagliati.
"""

from __future__ import annotations

from datetime import date, time

from app.ai import endpoint_health, model_selector, nl_query
from app.ai.client import _extract_json
from app.models import Mode


# ------------------------------------------------------------------- salute


def test_modello_ritirato_riconosciuto() -> None:
    """Il segnale piu' importante: `endpoints: []` significa che nessun provider
    serve piu' quel modello. E' successo davvero a mezzo pool nel giro di due
    mesi, e senza questo controllo l'IA smetterebbe di funzionare in silenzio."""
    health = endpoint_health._parse("tizio/modello:free", {"data": {"endpoints": []}})
    assert not health.alive
    assert "nessun provider" in health.detail


def test_endpoint_tutti_in_errore_e_bocciato() -> None:
    payload = {"data": {"endpoints": [{"status": -1, "uptime_last_5m": 100.0}]}}
    assert not endpoint_health._parse("x/y", payload).alive


def test_disponibilita_troppo_bassa_e_bocciata() -> None:
    payload = {"data": {"endpoints": [{"status": 0, "uptime_last_5m": 40.0}]}}
    health = endpoint_health._parse("x/y", payload)
    assert not health.alive
    assert "40" in health.detail


def test_modello_sano_promosso_col_provider_migliore() -> None:
    payload = {
        "data": {
            "endpoints": [
                {"status": 0, "uptime_last_5m": 91.0, "provider_name": "Lento"},
                {"status": 0, "uptime_last_5m": 99.5, "provider_name": "Veloce"},
            ]
        }
    }
    health = endpoint_health._parse("x/y", payload)
    assert health.alive
    assert health.uptime_5m == 99.5
    assert "Veloce" in health.providers


def test_le_fasce_sono_relative_al_migliore() -> None:
    """Con soglie assolute 99,8 e 100,0 finirebbero in fasce diverse solo perche'
    il confine cade li': due valori praticamente identici verrebbero separati."""
    assert endpoint_health.tiers([100.0, 99.8, 99.2]) == [0, 0, 0]
    assert endpoint_health.tiers([100.0, 95.0])[1] > 0


# ------------------------------------------------------- scelta del modello


def test_taglia_letta_dallo_slug() -> None:
    assert model_selector.parse_size_b("google/gemma-4-31b-it:free") == 31.0
    # Nei MoE lo slug porta due numeri: la taglia e i parametri attivi.
    assert model_selector.parse_size_b("google/gemma-4-26b-a4b-it:free") == 26.0
    assert model_selector.parse_size_b("nvidia/nemotron-3-super-120b-a12b:free") == 120.0
    assert model_selector.parse_size_b("inclusionai/ling-3.0-flash:free") is None


def test_modelli_giocattolo_penalizzati() -> None:
    grande = model_selector.score_model_name("google/gemma-4-31b-it:free", "json")
    piccolo = model_selector.score_model_name("tizio/modellino-3b-it:free", "json")
    assert grande > piccolo


def test_reasoning_penalizzato_solo_dove_fa_danno() -> None:
    """Il ragionamento esteso brucia il budget di token in una catena nascosta e
    fa troncare il JSON. Sul testo discorsivo lo stesso modello va benissimo."""
    pulito = "nvidia/nemotron-3-nano-30b-a3b:free"
    ragionante = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

    delta_json = model_selector.score_model_name(
        pulito, "json"
    ) - model_selector.score_model_name(ragionante, "json")
    delta_testo = model_selector.score_model_name(
        pulito, "advice"
    ) - model_selector.score_model_name(ragionante, "advice")

    assert delta_json > delta_testo > 0


def test_per_il_json_la_taglia_in_eccesso_non_premia() -> None:
    medio = model_selector.score_model_name("google/gemma-4-31b-it:free", "json")
    gigante = model_selector.score_model_name("nvidia/nemotron-3-ultra-550b-a55b:free", "json")
    assert medio > gigante


# ------------------------------------------------- interpretazione risposte


def test_json_estratto_anche_da_un_blocco_di_codice() -> None:
    assert _extract_json('```json\n{"hubs": ["Bari"]}\n```') == {"hubs": ["Bari"]}
    assert _extract_json('Ecco: {"ok": true} spero vada bene') == {"ok": True}
    assert _extract_json("nessun json qui") is None
    # Un oggetto tagliato a meta' non deve passare per buono.
    assert _extract_json('{"hubs": ["Bari", "Nap') is None


# ------------------------------------------------------ query da linguaggio


def test_data_nel_passato_spostata_avanti() -> None:
    """Un modello che sbaglia l'anno manderebbe la ricerca su un giorno gia'
    passato, e l'errore non sarebbe visibile da nessuna parte."""
    oggi = date(2026, 7, 27)
    assert nl_query._date("2026-01-05", oggi).year == 2027
    assert nl_query._date("2026-08-14", oggi) == date(2026, 8, 14)


def test_data_assurda_ignorata() -> None:
    oggi = date(2026, 7, 27)
    fallback = nl_query._date("2099-01-01", oggi)
    assert oggi < fallback <= date(2026, 8, 10)
    assert nl_query._date("domani", oggi) > oggi


def test_orari_e_mezzi_validati() -> None:
    assert nl_query._time("21:00") == time(21, 0)
    assert nl_query._time("sera") is None
    assert nl_query._modes(["rail", "bus"]) == {Mode.RAIL, Mode.BUS}
    # Valori inventati dal modello non devono restringere la ricerca a nulla.
    assert nl_query._modes(["teletrasporto"]) == {Mode.RAIL, Mode.BUS, Mode.AIR, Mode.FERRY}
    assert nl_query._modes(None) == {Mode.RAIL, Mode.BUS, Mode.AIR, Mode.FERRY}


def test_passeggeri_entro_limiti() -> None:
    assert nl_query._int(3, default=1, low=1, high=9) == 3
    assert nl_query._int(99, default=1, low=1, high=9) == 9
    assert nl_query._int("molti", default=1, low=1, high=9) == 1
