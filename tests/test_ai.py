"""La logica deterministica dell'integrazione IA, senza chiamare nessun modello.

Quello che si verifica qui e' la parte che decide *quale* modello usare e come
interpretare quello che risponde. E' il punto in cui gli errori sono silenziosi:
un JSON troncato che sembra valido, o un modello morto tenuto in lista, non
danno errore, danno risultati sbagliati.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time
from types import SimpleNamespace

import pytest

from app import config
from app.ai import (
    advisor,
    anthropic_client,
    client,
    endpoint_health,
    model_selector,
    nl_query,
    providers,
)
from app.ai.client import _extract_json
from app.config import get_settings
from app.models import (
    RISK_TEXT,
    AdviceCandidate,
    AdviceOption,
    AdviceRequest,
    CompareRequest,
    Mode,
    RiskFlag,
    TripStage,
    chain_dates,
)


@pytest.fixture
def solo(monkeypatch):
    """Configura solo i fornitori indicati, azzerando tutti gli altri.

    `get_settings` e' in cache: senza svuotarla le variabili nuove non
    arriverebbero mai, e il test passerebbe leggendo il `.env` di chi lo lancia.
    Per lo stesso motivo si neutralizza `data/local_secrets.json`: da quando le
    chiavi si salvano dal sito, una chiave vera sul portatile di chi lancia la
    suite renderebbe verdi test che devono girare senza."""

    def apply(**env: str):
        monkeypatch.setattr(config, "read_local_secrets", dict)
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


@pytest.mark.parametrize("uptime, alive", [(0, False), (79.9, False), (80, True), (100, True)])
def test_salute_soglia_compreso_zero(uptime, alive) -> None:
    payload = {"data": {"endpoints": [{"status": 0, "uptime_last_5m": uptime}]}}
    assert endpoint_health._parse("x/y", payload).alive is alive


@pytest.mark.parametrize("endpoint", [
    {"uptime_last_5m": 100}, {"status": None, "uptime_last_5m": 100},
    {"status": 0}, {"status": 0, "uptime_last_5m": None},
    {"status": 0, "uptime_last_5m": "100"},
    {"status": 0, "uptime_last_5m": float("nan")},
    {"status": False, "uptime_last_5m": 100},
    {"status": "0", "uptime_last_5m": 100},
])
def test_salute_dati_mancanti_non_sono_morte_ne_salute_confermata(endpoint) -> None:
    health = endpoint_health._parse("x/y", {"data": {"endpoints": [endpoint]}})
    assert health.alive  # resta candidato per il failover, senza promozione
    assert health.uptime_5m == 0
    assert "non verificato" in health.detail


@pytest.mark.parametrize("payload", [None, {}, {"data": {}}, {"data": {"endpoints": {}}}])
def test_salute_payload_malformato_non_ritira_un_modello(payload) -> None:
    health = endpoint_health._parse("x/y", payload)
    assert health.alive
    assert "non verificato" in health.detail


def test_salute_provider_in_errore_non_alza_uptime() -> None:
    health = endpoint_health._parse("x/y", {"data": {"endpoints": [
        {"status": -1, "uptime_last_5m": 100},
        {"status": 0, "uptime_last_5m": 0},
    ]}})
    assert not health.alive
    assert health.uptime_5m == 0


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


# ----------------------------------------------------------- viaggio a tappe


def _tappa(origin: str, destination: str, giorno: date, sosta: int = 0) -> TripStage:
    return TripStage(origin=origin, destination=destination, date=giorno, stay_days=sosta)


def test_la_sosta_sposta_la_tappa_successiva() -> None:
    """Il punto per cui un viaggio a tappe non e' due ricerche affiancate: la
    data della seconda **dipende** dalla prima."""
    tappe = chain_dates([
        _tappa("Matera", "Roma", date(2026, 8, 24), sosta=3),
        _tappa("Roma", "Torino", date(2026, 8, 24)),
    ])
    assert [t.date for t in tappe] == [date(2026, 8, 24), date(2026, 8, 27)]


def test_un_notturno_sposta_tutta_la_coda_del_viaggio() -> None:
    """Se la prima tappa arriva il giorno dopo, contare sulla partenza farebbe
    ripartire il viaggio con un giorno di anticipo — cioe' prima di essere
    arrivati."""
    tappe = chain_dates(
        [
            _tappa("Matera", "Roma", date(2026, 8, 24), sosta=3),
            _tappa("Roma", "Torino", date(2026, 8, 24)),
        ],
        arrivals=[date(2026, 8, 25), None],
    )
    assert tappe[1].date == date(2026, 8, 28)


def test_una_data_esplicita_piu_avanti_non_viene_anticipata() -> None:
    tappe = chain_dates([
        _tappa("Matera", "Roma", date(2026, 8, 24)),
        _tappa("Roma", "Torino", date(2026, 9, 10)),
    ])
    assert tappe[1].date == date(2026, 9, 10)


def test_la_tappa_senza_origine_riparte_da_dove_e_arrivata() -> None:
    """«poi a Torino» non nomina la partenza: e' Roma, ed e' cosi' che si dice."""
    parsed = {
        "stages": [
            {"origin": "Matera", "destination": "Roma", "stay_days": 3},
            {"destination": "Torino"},
        ]
    }
    tappe = nl_query._stages(parsed, date(2026, 8, 24))

    assert [(t.origin, t.destination) for t in tappe] == [
        ("Matera", "Roma"), ("Roma", "Torino"),
    ]


def test_sull_ultima_tappa_la_sosta_non_significa_niente() -> None:
    parsed = {"stages": [{"origin": "Matera", "destination": "Roma", "stay_days": 3}]}
    assert nl_query._stages(parsed, date(2026, 8, 24))[0].stay_days == 0


def test_una_tappa_che_gira_a_vuoto_viene_scartata() -> None:
    """Un modello che ripete la stessa citta' produrrebbe una ricerca da Roma a
    Roma: nessun risultato e nessuna spiegazione."""
    parsed = {
        "stages": [
            {"origin": "Roma", "destination": "Roma"},
            {"origin": "Roma", "destination": "Torino"},
        ]
    }
    tappe = nl_query._stages(parsed, date(2026, 8, 24))
    assert [(t.origin, t.destination) for t in tappe] == [("Roma", "Torino")]


def test_si_accetta_anche_la_risposta_a_viaggio_singolo() -> None:
    """I modelli piu' piccoli rispondono nel vecchio formato piatto. Buttare una
    risposta buona per la forma sarebbe uno spreco."""
    parsed = {"origin": "Torino", "destination": "Matera"}
    tappe = nl_query._stages(parsed, date(2026, 8, 24))
    assert [(t.origin, t.destination) for t in tappe] == [("Torino", "Matera")]


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


def test_i_fornitori_a_pagamento_restano_spenti(solo) -> None:
    """Chi paga decide quando spendere: non lo si scopre dalla fattura.

    Metterli in fondo alla fila non bastava — una giornata storta dei gratuiti
    li faceva scattare lo stesso. Ora servono una chiave **e** un interruttore."""
    solo(OPENAI_API_KEY="k", GROQ_API_KEY="k", ANTHROPIC_API_KEY="k")
    assert [p.name for p in providers.configured()] == ["groq"]


def test_acceso_l_interruttore_i_gratuiti_restano_comunque_primi(solo) -> None:
    solo(
        OPENAI_API_KEY="k",
        GROQ_API_KEY="k",
        ANTHROPIC_API_KEY="k",
        ALLOW_PAID_PROVIDERS="true",
    )
    ordine = [p.name for p in providers.configured()]

    assert ordine[0] == "groq"
    assert set(ordine[1:]) == {"openai", "anthropic"}


def test_chiedere_un_fornitore_per_nome_e_gia_una_scelta(solo) -> None:
    """L'interruttore difende dalla spesa non voluta, non da quella voluta:
    scrivere `LLM_PROVIDER=openai` e' esplicito quanto premere un bottone."""
    solo(OPENAI_API_KEY="k", LLM_PROVIDER="openai")
    assert [p.name for p in providers.configured()] == ["openai"]


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


async def test_il_catalogo_separa_gratuiti_e_a_pagamento(monkeypatch) -> None:
    """I due gruppi si trattano in modo diverso: i gratuiti sono una dozzina e si
    elencano tutti, quelli a pagamento sono trecento e si mostrano solo a chi li
    ha accesi. Confonderli vuol dire o proporre una spesa a chi ha detto di no,
    o nascondere meta' dei modelli usabili."""
    payload = {
        "data": [
            {"id": "tizio/gratis:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "caio/costoso", "pricing": {"prompt": "0.0001", "completion": "0.0002"}},
            {"id": "sempronio/embed-large", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "senza-prezzo"},
        ]
    }

    async def get_json(*args, **kwargs):
        return payload

    monkeypatch.setattr(
        model_selector, "get_http_client", lambda: SimpleNamespace(get_json=get_json)
    )
    from app.orchestrator import cache

    async def niente(*args, **kwargs):
        return None

    monkeypatch.setattr(cache, "get", niente)
    monkeypatch.setattr(cache, "set", niente)

    diviso = await model_selector.catalogo_openrouter()

    assert diviso["free"] == ["tizio/gratis:free"]
    assert diviso["paid"] == ["caio/costoso"]
    # "embed" e' nella lista nera: non e' un modello conversazionale.
    assert "sempronio/embed-large" not in diviso["free"]
    # Senza prezzo non si sa se costa: fuori da entrambi, che e' l'unica
    # risposta onesta.
    assert "senza-prezzo" not in diviso["free"] + diviso["paid"]


def test_il_troncamento_di_anthropic_si_chiama_altrimenti() -> None:
    """`max_tokens` la' e' `length` qui. Senza la traduzione la penalita' piu'
    importante del progetto non scatterebbe mai su quel fornitore."""
    assert anthropic_client.finish_reason("max_tokens") == "length"
    assert anthropic_client.finish_reason("end_turn") == "end_turn"


async def test_il_429_scade_prima_del_troncamento() -> None:
    """Un 429 non e' un difetto del modello: e' il throttle dell'host, condiviso
    e temporaneo. Tenerlo mezz'ora come un troncamento spegne un modello sano per
    il resto della sessione, ed e' quello che era successo a tutto il pool."""
    await model_selector.clear_penalties()
    await model_selector.record_penalty("veloce", "rate_limited", provider="prova")
    await model_selector.record_penalty("rotto", "truncated", provider="prova")

    scadenze = await _scadenze("prova")
    assert scadenze["veloce"] < scadenze["rotto"]

    await model_selector.clear_penalties()


async def test_un_429_non_accorcia_la_penalita_di_un_troncamento() -> None:
    """L'`ON CONFLICT` sovrascriveva la scadenza con l'ultima arrivata: un 429
    dopo un troncamento avrebbe rimesso in gioco un modello che tronca."""
    await model_selector.clear_penalties()
    await model_selector.record_penalty("misto", "truncated", provider="prova")
    prima = (await _scadenze("prova"))["misto"]
    await model_selector.record_penalty("misto", "rate_limited", provider="prova")
    dopo = (await _scadenze("prova"))["misto"]

    assert dopo >= prima

    await model_selector.clear_penalties()


async def _scadenze(provider: str) -> dict[str, float]:
    from app.orchestrator.db import get_db

    db = await get_db()
    async with db.execute(
        "SELECT model, expires_at FROM model_penalty WHERE provider = ?", (provider,)
    ) as cursor:
        return {model: scadenza for model, scadenza in await cursor.fetchall()}


# -------------------------------------------- le chiavi salvate dal sito


@pytest.fixture
def segreti(tmp_path, monkeypatch):
    """Sposta `data/local_secrets.json` in una cartella usa e getta.

    Senza, la suite scriverebbe nel file vero di chi la lancia — e un test che
    cancella una chiave sarebbe un test che gli cancella la chiave."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "secrets_path", lambda: tmp_path / config.LOCAL_SECRETS_FILE)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_la_chiave_salvata_dal_sito_vince_sul_dotenv(segreti, monkeypatch) -> None:
    """Chi ha appena premuto Salva si aspetta che valga adesso."""
    monkeypatch.setenv("GROQ_API_KEY", "dal-file-env")
    get_settings.cache_clear()
    assert get_settings().groq_api_key == "dal-file-env"

    config.save_local_secrets({"groq_api_key": "dal-sito"})
    get_settings.cache_clear()
    assert get_settings().groq_api_key == "dal-sito"


def test_la_stringa_vuota_cancella_e_il_campo_assente_no(segreti) -> None:
    config.save_local_secrets({"groq_api_key": "a", "openrouter_api_key": "b"})
    # Salvare solo una delle due non deve far sparire l'altra: l'interfaccia
    # manda il campo che hai toccato, non tutto il modulo.
    config.save_local_secrets({"groq_api_key": ""})
    rimasto = config.read_local_secrets()

    assert "groq_api_key" not in rimasto
    assert rimasto["openrouter_api_key"] == "b"


def test_dal_file_si_scrive_solo_quello_che_e_previsto(segreti) -> None:
    """Un JSON vecchio o modificato a mano non deve poter cambiare la
    concorrenza della ricerca o il livello di log."""
    config.save_local_secrets({"groq_api_key": "a", "search_max_concurrency": 9999})
    assert "search_max_concurrency" not in config.read_local_secrets()

    get_settings.cache_clear()
    assert get_settings().search_max_concurrency != 9999


def test_un_file_dei_segreti_rotto_non_impedisce_l_avvio(segreti) -> None:
    (segreti / config.LOCAL_SECRETS_FILE).write_text("{non json", encoding="utf-8")
    get_settings.cache_clear()

    assert config.read_local_secrets() == {}
    assert get_settings().log_level  # l'applicazione parte lo stesso


def test_lo_slug_del_modello_si_divide_alla_prima_barra(segreti) -> None:
    """`openrouter/google/gemma-4-31b-it:free` e' un fornitore e un modello, e il
    modello di barre ne ha una sua: dividere su tutte lo spezzerebbe."""
    config.save_local_secrets({"advice_model": "openrouter/google/gemma-4-31b-it:free"})
    get_settings.cache_clear()

    assert config.pinned_model("advice") == ("openrouter", "google/gemma-4-31b-it:free")
    assert config.pinned_model("json") is None


def test_il_modello_scelto_va_in_testa_ma_gli_altri_restano(solo, monkeypatch) -> None:
    """In testa e non da solo: se quello scelto rifiuta, restare senza risposta
    per rispettare la scelta sarebbe un modo curioso di rispettarla."""
    solo(GROQ_API_KEY="k")
    monkeypatch.setattr(
        config, "pinned_model", lambda task: ("groq", "llama-3.1-8b-instant")
    )
    groq = providers.BY_NAME["groq"]
    candidati = [(groq, "llama-3.3-70b-versatile"), (groq, "llama-3.1-8b-instant")]

    ordine = model_selector._pin_first(candidati, "json")

    assert [modello for _, modello in ordine] == [
        "llama-3.1-8b-instant", "llama-3.3-70b-versatile",
    ]


def test_un_modello_scelto_fuori_pool_torna_in_gioco(solo, monkeypatch) -> None:
    """La preferenza puo' aggiungere un modello fuori pool su un altro host."""
    solo(GROQ_API_KEY="k")
    monkeypatch.setattr(config, "pinned_model", lambda task: ("groq", "modello-raro"))
    groq = providers.BY_NAME["groq"]

    ordine = model_selector._pin_first([(groq, "llama-3.3-70b-versatile")], "json")

    assert ordine[0][1] == "modello-raro"
    assert len(ordine) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("alive", [False, True])
async def test_pin_openrouter_rispetta_salute(solo, monkeypatch, alive):
    solo(OPENROUTER_API_KEY="k")
    monkeypatch.setattr(config, "pinned_model", lambda task: ("openrouter", "pinned/model:free"))

    async def models(provider, task):
        return ["good/model:free"]

    async def health(slug):
        assert slug == "pinned/model:free"
        return endpoint_health.Health(slug, alive, 0, 0)

    monkeypatch.setattr(model_selector, "_models_for", models)
    monkeypatch.setattr(endpoint_health, "check", health)
    ordered = await model_selector.rank_candidates("json")
    assert ("pinned/model:free" in [model for _, model in ordered]) is alive
    assert "good/model:free" in [model for _, model in ordered]


def test_un_modello_scelto_su_un_fornitore_spento_viene_ignorato(solo, monkeypatch) -> None:
    solo(GROQ_API_KEY="k")
    monkeypatch.setattr(config, "pinned_model", lambda task: ("openai", "gpt-4.1"))
    groq = providers.BY_NAME["groq"]
    candidati = [(groq, "llama-3.3-70b-versatile")]

    assert model_selector._pin_first(candidati, "json") == candidati


@pytest.mark.parametrize("provider, model, key", [
    ("openai", "gpt-4.1", "OPENAI_API_KEY"),
    ("openrouter", "google/gemma-4-31b-it", "OPENROUTER_API_KEY"),
])
def test_pin_non_aggira_interruttore_pagamento(solo, monkeypatch, provider, model, key):
    solo(**{key: "k", "ALLOW_PAID_PROVIDERS": "false"})
    monkeypatch.setattr(config, "pinned_model", lambda task: (provider, model))
    assert model_selector._pin_first([], "json") == []


@pytest.mark.asyncio
async def test_pool_tutto_morto_non_viene_riprovato(solo, monkeypatch):
    solo(OPENROUTER_API_KEY="k")

    async def health(slugs):
        return {slug: endpoint_health.Health(slug, False, 0, 0) for slug in slugs}

    async def discovered(**kwargs):
        return []

    async def penalties(provider):
        return {}

    monkeypatch.setattr(endpoint_health, "check_many", health)
    monkeypatch.setattr(model_selector, "discover_free_models", discovered)
    monkeypatch.setattr(model_selector, "current_penalties", penalties)
    assert await model_selector.rank_models("json", ["retired/model:free"]) == []


def test_l_interruttore_dei_pagamento_si_salva_come_booleano(segreti) -> None:
    config.save_local_secrets({"allow_paid_providers": True})
    get_settings.cache_clear()
    assert get_settings().allow_paid_providers is True


# ------------------------------------------- il fallimento smette di tacere


@pytest.fixture
def catena(monkeypatch, solo):
    """Una catena finta di coppie (fornitore, modello), per esercitare
    `client.complete` senza rete, senza database e senza attese vere."""

    def apply(chiamata, *, quanti: int = 3, dorme: bool = False):
        solo(GROQ_API_KEY="k")
        groq = providers.BY_NAME["groq"]
        coppie = [(groq, f"modello-{i}") for i in range(quanti)]

        async def rank(task: str):
            return coppie

        penalita: list[tuple[str, str, str]] = []

        async def record(model: str, reason: str, provider: str = "openrouter"):
            penalita.append((provider, model, reason))

        attese: list[float] = []

        async def finta_attesa(secondi: float):
            attese.append(secondi)

        monkeypatch.setattr(model_selector, "rank_candidates", rank)
        monkeypatch.setattr(model_selector, "record_penalty", record)
        monkeypatch.setattr(client, "_call", chiamata)
        if not dorme:
            monkeypatch.setattr(asyncio, "sleep", finta_attesa)
        return SimpleNamespace(penalita=penalita, attese=attese)

    return apply


async def test_senza_chiavi_dice_perche_invece_di_sparire(solo) -> None:
    """Il difetto vero: «non configurata» e «ha fallito» erano lo stesso `None`,
    e l'interfaccia non aveva modo di dire quale delle due."""
    solo()
    risposta = await client.complete("advice", "s", "u")

    assert not risposta.ok
    assert risposta.reason == client.NOT_CONFIGURED
    assert "chiave" in client.why(risposta.reason)


async def test_il_429_fa_riaspettare_lo_stesso_modello(catena) -> None:
    """La correzione al burst: il primo modello va aspettato, non scartato. Prima
    si passava subito al successivo e in sei secondi il pool era bruciato."""
    tentativi: list[str] = []

    async def chiamata(provider, model, *args, **kwargs):
        tentativi.append(model)
        if len(tentativi) == 1:
            raise providers.CallFailed("rate_limited", "HTTP 429 da openrouter.ai")
        return "va bene cosi'", "stop"

    spia = catena(chiamata)
    risposta = await client.complete("advice", "s", "u")

    assert risposta.ok
    assert risposta.text == "va bene cosi'"
    # Lo stesso modello, due volte: nessun altro e' stato toccato.
    assert tentativi == ["modello-0", "modello-0"]
    assert spia.attese == [client.RETRY_WAITS[0]]
    # E soprattutto: nessuna penalita' per un throttle passeggero.
    assert spia.penalita == []


async def test_quando_tutti_rifiutano_il_motivo_arriva_intero(catena) -> None:
    async def chiamata(provider, model, *args, **kwargs):
        raise providers.CallFailed("rate_limited", "HTTP 429 da openrouter.ai")

    spia = catena(chiamata, quanti=2)
    risposta = await client.complete("advice", "s", "u")

    assert not risposta.ok
    assert risposta.reason == client.RATE_LIMITED
    assert "429" in client.why(risposta.reason)
    # Le attese finiscono, poi si penalizza: una volta per modello, non a ogni giro.
    assert [modello for _, modello, _ in spia.penalita] == ["modello-0", "modello-1"]


async def test_il_troncamento_resta_un_difetto_del_modello(catena) -> None:
    """`finish_reason == "length"`: la risposta esiste ma e' tagliata. Qui non si
    riprova, si penalizza e si cambia modello — ed e' il motivo che arriva."""
    async def chiamata(provider, model, *args, **kwargs):
        return "meta' fra", "length"

    spia = catena(chiamata, quanti=2)
    risposta = await client.complete("advice", "s", "u")

    assert not risposta.ok
    assert risposta.reason == client.TRUNCATED
    assert spia.attese == []
    assert [motivo for _, _, motivo in spia.penalita] == ["truncated", "truncated"]


def test_il_ragionamento_al_posto_della_risposta_si_riconosce() -> None:
    """Visto sul campo: «We need to produce indicates ,, has is,,., and,» stampato
    come se fosse un consiglio di viaggio. Non e' vuoto, non e' troncato, e
    nessun altro controllo lo prende."""
    assert client.sembra_ragionamento("We need to produce indicates ,, has is,,., and,")
    assert client.sembra_ragionamento("Let me analyze the options carefully...")
    assert client.sembra_ragionamento("The user wants a trip from Matera")

    # E non deve scattare su una risposta buona.
    buona = (
        "Io prenderei il pullman delle 23:35: costa la meta' del treno e ti fa "
        "risparmiare una notte d'albergo, visto che arrivi la mattina presto. "
        "Il volo conviene solo se hai fretta e non ti pesa il trasferimento."
    )
    assert not client.sembra_ragionamento(buona)
    # Neanche su una risposta cortissima, dove il campione non dice niente.
    assert not client.sembra_ragionamento("Prendi il pullman.")


async def test_il_ragionamento_fa_cambiare_modello(catena) -> None:
    tentativi: list[str] = []

    async def chiamata(provider, model, *args, **kwargs):
        tentativi.append(model)
        if len(tentativi) == 1:
            return "We need to produce the answer about trains and buses here", "stop"
        return "Io prenderei il pullman delle 23:35, costa meno e non perdi la giornata.", "stop"

    spia = catena(chiamata, quanti=2)
    risposta = await client.complete("advice", "s", "u")

    assert risposta.ok
    assert risposta.text.startswith("Io prenderei")
    assert [motivo for _, _, motivo in spia.penalita] == ["garbled"]


async def test_una_risposta_sbagliata_in_cache_non_viene_servita(catena, monkeypatch) -> None:
    """Il controllo deve valere anche sulla cache, che e' il caso in cui costa
    di piu': una risposta rotta salvata si ripropone identica a ogni ricarica."""
    from app.orchestrator import cache

    async def cache_get(key):
        return {"text": "We need to produce the answer", "model": "m", "provider": "p"}

    async def cache_set(*args, **kwargs):
        return None

    monkeypatch.setattr(cache, "get", cache_get)
    monkeypatch.setattr(cache, "set", cache_set)

    async def chiamata(provider, model, *args, **kwargs):
        return "Io prenderei il treno delle 7, arriva prima e costa uguale.", "stop"

    catena(chiamata)
    risposta = await client.complete("advice", "s", "u", cache_key="qualunque")

    assert risposta.text.startswith("Io prenderei")


async def test_sul_json_il_controllo_non_scatta(catena) -> None:
    """Un JSON non e' in italiano e non deve esserlo: passarlo di qui lo
    boccerebbe sempre, e l'interpretazione della frase smetterebbe di funzionare."""
    async def chiamata(provider, model, *args, **kwargs):
        return '{"origin": "Torino", "destination": "Matera"}', "stop"

    spia = catena(chiamata)
    risposta = await client.complete("json", "s", "u", json_mode=True)

    assert risposta.ok
    assert spia.penalita == []


async def test_le_chiamate_allo_stesso_fornitore_vanno_in_fila(catena) -> None:
    """Il burst che aveva zittito l'IA: un consiglio per colonna, il confronto e
    gli scali partivano insieme sullo stesso host. Ora c'e' una fila."""
    dentro = 0
    massimo = 0

    async def chiamata(provider, model, *args, **kwargs):
        nonlocal dentro, massimo
        dentro += 1
        massimo = max(massimo, dentro)
        await asyncio.sleep(0)  # da' modo alle altre di provarci
        dentro -= 1
        return "ok", "stop"

    catena(chiamata, dorme=True)
    await asyncio.gather(*(client.complete("advice", "s", "u") for _ in range(5)))

    assert massimo == 1


def test_ogni_motivo_ha_la_sua_frase() -> None:
    """I motivi arrivano fino a schermo e sono cose diverse: «manca la chiave» e
    «il fornitore ha rifiutato» si risolvono in due modi. Un motivo senza frase
    diventerebbe in silenzio «nessun modello ha risposto»."""
    motivi = {
        client.NOT_CONFIGURED, client.NO_MODELS, client.RATE_LIMITED,
        client.TRUNCATED, client.GARBLED, client.INVENTED, client.UNANCHORED,
        client.FAILED,
    }
    assert motivi <= set(client.WHY)
    # `nothing` non e' un guasto e non ha frase: lo riconosce la pagina, per
    # tacere invece di dare la colpa all'IA.
    assert client.NOTHING not in client.WHY


# ------------------------------------------- il consiglio dice di quale parla
#
# Il difetto da cui nasce tutto: il modello scriveva «la Torino → Matera 4,
# 120,80 euro» e a schermo nessun 4 esisteva. La numerazione viveva solo dentro
# il prompt, quindi non era verificabile e nessuno poteva accorgersi che fosse
# sbagliata.


def _opzione(ref: str, **extra) -> AdviceOption:
    base = {
        "ref": ref,
        "depart": datetime(2026, 8, 7, 18, 30),
        "arrive": datetime(2026, 8, 8, 8, 45),
        "duration_min": 855,
        "total": 80.0,
        "modes": ["bus"],
        "operators": ["Marino Autolinee"],
        "n_changes": 0,
        "n_tickets": 1,
    }
    return AdviceOption(**{**base, **extra})


def _richiesta(**extra) -> AdviceRequest:
    base = {
        "origin": "Torino",
        "destination": "Matera",
        "date": date(2026, 8, 7),
        "options": [_opzione("1"), _opzione("2", total=24.98)],
        "found": 25,
    }
    return AdviceRequest(**{**base, **extra})


@pytest.fixture
def prompt_visto(monkeypatch):
    """Cattura il prompt spedito, senza chiamare nessun modello."""
    visti: list[str] = []

    async def complete(task, system, user, **kwargs):
        visti.append(f"{system}\n---\n{user}")
        return client.Answer(
            completion=client.Completion(text="Prenderei la [1].", model="m", finish_reason="stop")
        )

    monkeypatch.setattr(client, "complete", complete)
    monkeypatch.setattr(client, "is_configured", lambda: True)
    return visti


async def test_ogni_soluzione_arriva_al_modello_col_numero_della_scheda(prompt_visto) -> None:
    """Il riferimento e' il numero scritto sulla scheda: e' l'unico modo che ha
    il modello di nominare una soluzione in modo verificabile."""
    await advisor.advise(_richiesta())

    assert "[1] 07/08 18:30" in prompt_visto[0]
    assert "[2] 07/08 18:30" in prompt_visto[0]


async def test_il_modello_sa_quante_soluzioni_ci_sono_davvero(prompt_visto) -> None:
    """Ne vedeva sei e le credeva tutte: «e' l'unica sotto i 50 euro» era falso
    rispetto alle altre diciannove che l'utente poteva scorrere."""
    await advisor.advise(_richiesta())

    assert "le prime 2 per punteggio, su 25 trovate" in prompt_visto[0]


async def test_l_arrivo_del_giorno_dopo_porta_la_sua_data(prompt_visto) -> None:
    """La scheda scrive `+1`. Il prompt scriveva la sola ora, e un arrivo alle
    08:45 del giorno dopo diventava un arrivo in giornata."""
    await advisor.advise(_richiesta())

    assert "18:30 - 08/08 08:45" in prompt_visto[0]


async def test_i_vincoli_saltati_arrivano_al_modello(prompt_visto) -> None:
    """Il difetto piu' pericoloso della famiglia: la pagina dichiara che nessuna
    soluzione rispetta il budget, il prompt continuava a dire «budget massimo
    120 euro» sopra una lista che lo sforava tutta."""
    await advisor.advise(
        _richiesta(max_budget=120, relaxed=[{"kind": "max_budget", "value": 120}])
    )

    assert "ATTENZIONE" in prompt_visto[0]
    assert "budget massimo 120 euro" in prompt_visto[0]


async def test_il_budget_e_detto_a_persona(prompt_visto) -> None:
    """Il motore filtra per persona. Senza dirlo, il modello moltiplicava per i
    passeggeri e annunciava uno sforamento che non c'era."""
    await advisor.advise(_richiesta(max_budget=120, pax=3))

    assert "Budget massimo: 120 euro a persona" in prompt_visto[0]


async def test_gli_avvisi_arrivano_in_italiano(prompt_visto) -> None:
    """`last_leg_unverified` dentro un prompt italiano non e' un avviso, e' un
    codice: a schermo l'utente legge una frase compiuta."""
    await advisor.advise(
        _richiesta(options=[_opzione("1", flags=["separate_tickets"]), _opzione("2")])
    )

    assert "biglietti separati" in prompt_visto[0]
    assert "separate_tickets" not in prompt_visto[0]


async def test_l_andata_e_ritorno_non_somma_le_durate(prompt_visto) -> None:
    """La pagina mandava gli orari della sola andata con la durata di andata +
    ritorno: un numero che a schermo non compariva da nessuna parte."""
    await advisor.advise(
        _richiesta(
            options=[
                _opzione(
                    "1",
                    return_depart=datetime(2026, 8, 10, 9, 0),
                    return_arrive=datetime(2026, 8, 10, 14, 30),
                    return_duration_min=330,
                ),
                _opzione("2"),
            ]
        )
    )

    assert "(14h15)" in prompt_visto[0]  # l'andata, non la somma
    assert "ritorno 10/08 09:00 - 10/08 14:30 (5h30)" in prompt_visto[0]


async def test_le_differenze_arrivano_gia_calcolate(prompt_visto) -> None:
    """Il modello sbaglia le sottrazioni: a schermo ha scritto «ti fa
    risparmiare quasi due ore» fra 12h35 e 12h30. I dati erano giusti, il conto
    no — quindi il conto non glielo si fa fare."""
    await advisor.advise(
        _richiesta(
            options=[
                _opzione("1", total=80.0, duration_min=855),  # 14h15
                _opzione("2", total=74.60, duration_min=750),  # 12h30
            ]
        )
    )

    assert "la più economica e la più rapida" in prompt_visto[0]
    assert "+5,40 euro della più economica, +1h45 della più rapida" in prompt_visto[0]


async def test_anche_cambi_e_biglietti_arrivano_contati(prompt_visto) -> None:
    """Gli altri due paragoni che il consiglio fa di continuo. Si dicono solo
    quando c'e' una differenza: su ogni riga sarebbero rumore, e i numeri
    assoluti la riga li porta gia'."""
    await advisor.advise(
        _richiesta(
            options=[
                _opzione("1", n_changes=0, n_tickets=1),
                _opzione("2", total=60.0, n_changes=2, n_tickets=3),
            ]
        )
    )

    assert "+2 cambi della più diretta" in prompt_visto[0]
    assert "+2 biglietti da comprare a parte" in prompt_visto[0]
    # La soluzione diretta non si porta dietro un «+0 cambi».
    assert "+0 cambi" not in prompt_visto[0]


async def test_i_pari_merito_sono_entrambi_i_migliori(prompt_visto) -> None:
    """Confrontando gli oggetti invece dei valori, la seconda soluzione dallo
    stesso prezzo sarebbe risultata «piu' cara di zero euro» della prima."""
    await advisor.advise(
        _richiesta(options=[_opzione("1", total=24.98), _opzione("2", total=24.98)])
    )

    assert prompt_visto[0].count("la più economica") == 2


async def test_su_andata_e_ritorno_il_confronto_guarda_il_viaggio_intero(
    prompt_visto,
) -> None:
    """Nella riga le due tratte restano separate — sommarle produceva un numero
    che a schermo non c'era — ma per dire quale dura di meno contano insieme."""
    await advisor.advise(
        _richiesta(
            options=[
                _opzione(
                    "1",
                    duration_min=60,
                    return_depart=datetime(2026, 8, 10, 9, 0),
                    return_arrive=datetime(2026, 8, 10, 15, 0),
                    return_duration_min=360,  # sei ore di ritorno: in tutto 7h
                ),
                _opzione("2", duration_min=120, total=99.0),  # due ore in tutto
            ]
        )
    )

    assert "la più economica, +5h00 della più rapida" in prompt_visto[0]


async def test_una_soluzione_citata_che_non_esiste_fa_scartare_la_risposta(catena) -> None:
    """La rete di sicurezza contro il difetto originale. Un riferimento
    inventato non e' un dettaglio di stile: manda a cercare a schermo una
    scheda che non c'e'."""
    tentativi: list[str] = []

    async def chiamata(provider, model, *args, **kwargs):
        tentativi.append(model)
        if len(tentativi) == 1:
            return "Prenderei senza dubbio la [9], costa poco.", "stop"
        return "Prenderei la [1], e' la piu' comoda.", "stop"

    spia = catena(chiamata)
    risposta = await advisor.advise(_richiesta())

    assert risposta.ok
    assert "[1]" in risposta.text
    assert spia.penalita == [("groq", "modello-0", client.INVENTED)]


async def test_un_consiglio_che_non_nomina_nessuna_soluzione_si_scarta(catena) -> None:
    """«Io sceglierei l'opzione con Ryanair» con due voli Ryanair non si puo'
    seguire: e' il difetto visto a schermo, non una sfumatura."""
    async def chiamata(provider, model, *args, **kwargs):
        return "Sceglierei l'opzione con Ryanair, arriva prima di tutte.", "stop"

    spia = catena(chiamata, quanti=2)
    risposta = await advisor.advise(_richiesta())

    assert not risposta.ok
    assert risposta.reason == client.UNANCHORED
    assert len(spia.penalita) == 2


async def test_con_una_sola_soluzione_non_serve_nominarla(catena) -> None:
    """Non c'e' ambiguita' da sciogliere: pretendere il riferimento butterebbe
    un consiglio valido."""
    async def chiamata(provider, model, *args, **kwargs):
        return "E' l'unica soluzione trovata: parte alle 18:30 e costa 80 euro.", "stop"

    catena(chiamata)
    risposta = await advisor.advise(_richiesta(options=[_opzione("1")]))

    assert risposta.ok


def test_la_chiave_del_confronto_tiene_conto_della_frase_scritta() -> None:
    """La chiave ignorava `raw_text` e la valigia, che pero' finiscono nel
    prompt: per un'ora si serviva la risposta a una domanda diversa."""
    from app.orchestrator import cache

    def chiave(**extra):
        richiesta = CompareRequest(
            candidates=[
                AdviceCandidate(
                    label=f"Meta {i}",
                    origin="Torino",
                    destination=f"Meta {i}",
                    date=date(2026, 8, 7),
                    options=[_opzione("1")],
                )
                for i in (1, 2)
            ],
            **extra,
        )
        return cache.make_key(
            "ai:compare",
            richiesta.pax,
            richiesta.max_budget,
            richiesta.with_checked_bag,
            richiesta.raw_text,
            sorted(
                f"{c.label}|{c.date}|{c.return_date}|{c.found}|{c.partial}|"
                + ";".join(
                    f"{o.ref}/{o.depart:%d%H%M}/{o.total:.2f}/{o.n_changes}/{o.n_tickets}"
                    for o in c.options
                )
                for c in richiesta.candidates
            ),
        )

    assert chiave(raw_text="vado a Bari") != chiave(raw_text="vado a Bari con la bici")
    assert chiave(with_checked_bag=True) != chiave(with_checked_bag=False)


def test_ogni_avviso_ha_la_sua_frase_anche_nella_pagina() -> None:
    """Due mappe gemelle, una in Python e una in JavaScript. Se divergono, il
    modello e l'utente leggono due descrizioni diverse della stessa scheda —
    ed e' esattamente il difetto che questa versione toglie di mezzo."""
    from pathlib import Path

    sorgente = (Path(__file__).parent.parent / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    blocco = sorgente.split("const FLAG_TEXT = {", 1)[1].split("};", 1)[0]
    nella_pagina = set(re.findall(r"^\s*(\w+):", blocco, re.MULTILINE))

    assert nella_pagina == {flag.value for flag in RiskFlag}
    assert set(RISK_TEXT) == set(RiskFlag)


# ------------------------------------------------------------- aggiornamenti


def test_le_versioni_si_confrontano_a_numeri_non_a_lettere() -> None:
    """Confrontando le stringhe "0.9" risulterebbe piu' recente di "0.10", e chi
    ha la vecchia non vedrebbe mai l'aggiornamento."""
    from app import update

    assert update.piu_recente("v0.10.0", "0.9.9")
    assert update.piu_recente("0.2.0", "0.1.0")
    assert not update.piu_recente("0.1.0", "0.2.0")
    assert not update.piu_recente("v0.2.0", "0.2.0")


def test_il_tag_puo_avere_la_v_e_un_suffisso() -> None:
    from app import update

    assert update._versione_numerica("v1.2.3-beta") == (1, 2, 3)
    assert update._versione_numerica("garbage") == (0,)


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


# ------------------------------- la ricerca che chiede invece di indovinare


def _risposta_finta(payload: dict, ricevuti: list | None = None):
    """Un modello finto che restituisce quel JSON e registra cosa gli e' arrivato.

    Il finto dichiara **solo** i parametri che `complete_json` ha davvero, e
    raccoglie il resto in `extra`: cosi' il giorno in cui qualcuno riaprisse un
    canale laterale verso il modello — una storia, un esempio, un pezzo di
    stato — questi test lo vedono invece di lasciarlo passare. E' la stessa
    tesi di `_richiesta`: al modello arriva una richiesta e basta."""

    async def complete_json(task, system, user, *, max_tokens=700, **extra):
        if ricevuti is not None:
            ricevuti.append(extra)
        return client.Answer(
            completion=client.Completion(text="", model="finto", finish_reason="stop",
                                         provider="finto"),
            data=payload,
        )

    return complete_json


@pytest.mark.asyncio
async def test_senza_la_data_la_chiede_invece_di_inventarla(monkeypatch) -> None:
    """Il difetto che questa funzione viene a chiudere.

    «da Torino a Matera» senza quando veniva completato con una data decisa dal
    modello — quasi sempre oggi — e la ricerca partiva su un giorno che nessuno
    aveva chiesto. Una supposizione sbagliata non si vede da nessuna parte;
    una domanda si'."""
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", _risposta_finta({
        "stages": [{"origin": "Torino", "destination": "Matera"}],
        "date": "2026-08-22",
        "data_detta": False,
    }))

    with pytest.raises(nl_query.ServeAltro) as errore:
        await nl_query.parse("da Torino a Matera", today=date(2026, 8, 22))
    assert errore.value.domanda == "Per quando?"


@pytest.mark.asyncio
async def test_senza_le_tappe_chiede_anche_quelle(monkeypatch) -> None:
    """L'altra meta' della stessa idea, e nessuna delle due la scrive il modello.

    «voglio andare a Matera venerdi'» e' una frase a cui manca un pezzo, non una
    frase sbagliata: prima moriva con un 422 e bisognava riscriverla intera,
    buttando via anche la meta' capita. Che le tappe manchino lo sa `_stages`,
    in Python, senza chiedere niente a nessuno."""
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", _risposta_finta({
        "stages": [{"origin": None, "destination": "Matera"}],
        "date": "2026-08-28",
        "data_detta": True,
    }))

    with pytest.raises(nl_query.ServeAltro) as errore:
        await nl_query.parse("voglio andare a Matera venerdi", today=date(2026, 8, 22))
    assert errore.value.domanda == "Da dove parti e dove vai?"


@pytest.mark.asyncio
async def test_quando_manca_tutto_si_chiede_prima_il_quando(monkeypatch) -> None:
    """L'ordine dei due controlli non e' arbitrario, ed e' facile invertirlo.

    Quando il quando non c'e', il modello svuota **anche** le tappe: i due
    sintomi arrivano insieme. Chiedendo prima le tappe si chiederebbe «da dove
    parti?» a chi l'aveva scritto benissimo, che e' il modo peggiore di fare una
    domanda."""
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", _risposta_finta({
        "stages": [],
        "date": "2026-08-22",
        "data_detta": False,
    }))

    with pytest.raises(nl_query.ServeAltro) as errore:
        await nl_query.parse("da Torino a Matera", today=date(2026, 8, 22))
    assert errore.value.domanda == "Per quando?"


@pytest.mark.asyncio
async def test_un_guasto_resta_un_guasto_non_una_domanda(monkeypatch) -> None:
    """Il confine che tiene in piedi la conversazione.

    Se il modello non risponde, chiedere all'utente non serve a niente: non c'e'
    risposta che possa sbloccare la cosa, e la pagina continuerebbe a chiedere
    in eterno. Quello resta un errore con un motivo leggibile."""
    monkeypatch.setattr(client, "is_configured", lambda: True)

    async def muto(task, system, user, *, max_tokens=700, **extra):
        return client.Answer(reason=client.FAILED, detail="nessun modello")

    monkeypatch.setattr(client, "complete_json", muto)

    with pytest.raises(nl_query.ParseFailed):
        await nl_query.parse("da Torino a Matera venerdi", today=date(2026, 8, 22))


@pytest.mark.asyncio
async def test_con_i_dati_che_bastano_non_chiede_niente(monkeypatch) -> None:
    """Il rovescio, ed e' quello che rende la funzione sopportabile: un modello
    che chiedesse il budget avendo gia' partenza, meta e data farebbe perdere un
    giro a chi aveva scritto tutto. Se il viaggio sta in piedi si cerca."""
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", _risposta_finta({
        "stages": [{"origin": "Torino", "destination": "Matera"}],
        "date": "2026-08-28",
        "data_detta": True,
    }))

    plan = await nl_query.parse("da Torino a Matera venerdi", today=date(2026, 8, 22))
    assert plan.stages[0].origin == "Torino"
    assert plan.stages[0].destination == "Matera"


@pytest.mark.asyncio
async def test_i_turni_precedenti_arrivano_cuciti_in_una_frase(monkeypatch) -> None:
    """Senza il contesto la risposta «venerdi 28» non vuol dire niente.

    Arriva al modello cucita alla frase di prima, e non come messaggi separati.
    Misurato il 2026-08-22: con i turni separati — anche resi coerenti, con la
    domanda riscritta in JSON e un esempio a due turni nel prompt — il modello
    rispondeva all'aggiunta invece che al totale, dava la data giusta e
    `stages` vuoto, e Torino e Matera sparivano. La memoria la tiene la pagina,
    che puo' farlo con certezza."""
    visti: list = []

    async def complete_json(task, system, user, *, max_tokens=700, **extra):
        visti.append((user, extra))
        return client.Answer(
            completion=client.Completion(text="", model="finto", finish_reason="stop",
                                         provider="finto"),
            data={"stages": [{"origin": "Torino", "destination": "Matera"}],
                  "date": "2026-08-28"},
        )

    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", complete_json)

    storia = [
        {"role": "user", "content": "da Torino a Matera"},
        {"role": "assistant", "content": "Per quando?"},
    ]
    plan = await nl_query.parse("venerdi 28", today=date(2026, 8, 22), history=storia)

    (prompt, extra), = visti
    assert "da Torino a Matera. venerdi 28" in prompt
    assert not extra, "i turni non si mandano separati: non reggono"
    assert plan.raw_text == "da Torino a Matera. venerdi 28"


@pytest.mark.asyncio
async def test_senza_storia_il_comportamento_e_quello_di_prima(monkeypatch) -> None:
    """La conversazione e' un di piu': chi non la usa non deve accorgersene."""
    visti: list = []
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", _risposta_finta({
        "stages": [{"origin": "Torino", "destination": "Matera"}],
        "date": "2026-08-28",
    }, visti))

    await nl_query.parse("da Torino a Matera venerdi", today=date(2026, 8, 22))
    assert visti == [{}]  # niente oltre la richiesta, come prima


@pytest.mark.asyncio
async def test_un_modello_che_dimentica_il_booleano_non_blocca_la_ricerca(monkeypatch) -> None:
    """Il difetto piu' probabile e' l'omissione, non la bugia.

    Un modello che non mette `data_detta` deve comportarsi come prima, non
    fermare una ricerca che si poteva fare: si chiede solo quando la risposta
    e' un `false` esplicito."""
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", _risposta_finta({
        "stages": [{"origin": "Torino", "destination": "Matera"}],
        "date": "2026-08-28",
    }))

    plan = await nl_query.parse("da Torino a Matera venerdi", today=date(2026, 8, 22))
    assert plan.stages[0].destination == "Matera"
