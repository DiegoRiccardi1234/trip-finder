"""La logica deterministica dell'integrazione IA, senza chiamare nessun modello.

Quello che si verifica qui e' la parte che decide *quale* modello usare e come
interpretare quello che risponde. E' il punto in cui gli errori sono silenziosi:
un JSON troncato che sembra valido, o un modello morto tenuto in lista, non
danno errore, danno risultati sbagliati.
"""

from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace

import pytest

from app.ai import anthropic_client, client, endpoint_health, model_selector, nl_query, providers
from app.ai.client import _extract_json
from app.config import get_settings
from app.models import Mode


@pytest.fixture
def solo(monkeypatch):
    """Configura solo i fornitori indicati, azzerando tutti gli altri.

    `get_settings` e' in cache: senza svuotarla le variabili nuove non
    arriverebbero mai, e il test passerebbe leggendo il `.env` di chi lo lancia."""

    def apply(**env: str):
        for provider in providers.PROVIDERS:
            monkeypatch.delenv(provider.key_field.upper(), raising=False)
            monkeypatch.setenv(provider.key_field.upper(), "")
        monkeypatch.setenv("LLM_PROVIDER", "")
        monkeypatch.setenv("LLM_BASE_URL", "")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        return get_settings()

    yield apply
    get_settings.cache_clear()


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


# --------------------------------------------------------- fornitori multipli


def test_senza_chiavi_l_ia_e_spenta(solo) -> None:
    """La regola che regge tutto il modulo: l'IA e' facoltativa. Senza chiavi
    non deve sollevare niente, deve semplicemente non esserci."""
    solo()
    assert providers.configured() == []
    assert client.is_configured() is False


def test_si_configura_con_una_chiave_qualsiasi(solo) -> None:
    solo(GROQ_API_KEY="k")
    assert [p.name for p in providers.configured()] == ["groq"]
    assert client.is_configured() is True


def test_i_gratuiti_si_provano_per_primi(solo) -> None:
    """Chi paga decide quando spendere: non lo si scopre dalla fattura."""
    solo(OPENAI_API_KEY="k", GROQ_API_KEY="k", ANTHROPIC_API_KEY="k")
    ordine = [p.name for p in providers.configured()]

    assert ordine[0] == "groq"
    assert set(ordine[1:]) == {"openai", "anthropic"}


def test_llm_provider_forza_l_ordine_e_esclude(solo) -> None:
    solo(OPENROUTER_API_KEY="k", GROQ_API_KEY="k", LLM_PROVIDER="groq")
    assert [p.name for p in providers.configured()] == ["groq"]

    solo(OPENROUTER_API_KEY="k", GROQ_API_KEY="k", LLM_PROVIDER="groq,openrouter")
    assert [p.name for p in providers.configured()] == ["groq", "openrouter"]

    # Un nome sconosciuto viene ignorato: per una funzione facoltativa, far
    # cadere l'avvio per un refuso nel .env sarebbe una punizione sproporzionata.
    solo(GROQ_API_KEY="k", LLM_PROVIDER="tipografia,groq")
    assert [p.name for p in providers.configured()] == ["groq"]


def test_il_server_locale_non_vuole_una_chiave(solo) -> None:
    """L'unico fornitore che esiste per indirizzo e non per chiave: in locale
    non c'e' nessuno da autenticare, e pretenderla lo renderebbe inusabile."""
    solo(LLM_BASE_URL="http://localhost:11434/v1")
    assert [p.name for p in providers.configured()] == ["local"]


async def test_la_penalita_e_della_coppia_non_del_modello() -> None:
    """Lo stesso slug puo' troncare su un host e funzionare su un altro.
    Spegnerlo ovunque per colpa di uno solo sarebbe uno spreco."""
    await model_selector.clear_penalties()
    await model_selector.record_penalty("gpt-oss-120b", "truncated", provider="openrouter")

    assert "gpt-oss-120b" in await model_selector.current_penalties("openrouter")
    assert await model_selector.current_penalties("cerebras") == {}

    await model_selector.clear_penalties()


def test_il_troncamento_di_anthropic_si_chiama_altrimenti() -> None:
    """`max_tokens` la' e' `length` qui. Senza la traduzione la penalita' piu'
    importante del progetto non scatterebbe mai su quel fornitore."""
    assert anthropic_client.finish_reason("max_tokens") == "length"
    assert anthropic_client.finish_reason("end_turn") == "end_turn"


def test_il_testo_di_anthropic_sta_nei_blocchi_di_tipo_testo() -> None:
    """La risposta e' un elenco di blocchi: leggere il primo alla cieca funziona
    finche' non arriva un blocco di pensiero davanti, e quel giorno la risposta
    risulta vuota senza nessun errore."""
    message = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="  Prendi il pullman.  "),
        ]
    )
    assert anthropic_client._text(message) == "Prendi il pullman."
    assert anthropic_client._text(SimpleNamespace(content=[])) == ""
