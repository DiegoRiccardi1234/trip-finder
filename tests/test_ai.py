"""La logica deterministica dell'integrazione IA, senza chiamare nessun modello.

Quello che si verifica qui e' la parte che decide *quale* modello usare e come
interpretare quello che risponde. E' il punto in cui gli errori sono silenziosi:
un JSON troncato che sembra valido, o un modello morto tenuto in lista, non
danno errore, danno risultati sbagliati.
"""

from __future__ import annotations

import asyncio
from datetime import date, time
from types import SimpleNamespace

import pytest

from app import config
from app.ai import anthropic_client, client, endpoint_health, model_selector, nl_query, providers
from app.ai.client import _extract_json
from app.config import get_settings
from app.models import Mode, TripStage, chain_dates


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


def test_un_modello_scelto_ma_scartato_dalla_salute_torna_in_gioco(solo, monkeypatch) -> None:
    """Il controllo di salute lo aveva tolto dai candidati. Sceglierlo a mano e'
    una decisione esplicita e vale piu' di un'euristica."""
    solo(GROQ_API_KEY="k")
    monkeypatch.setattr(config, "pinned_model", lambda task: ("groq", "modello-raro"))
    groq = providers.BY_NAME["groq"]

    ordine = model_selector._pin_first([(groq, "llama-3.3-70b-versatile")], "json")

    assert ordine[0][1] == "modello-raro"
    assert len(ordine) == 2


def test_un_modello_scelto_su_un_fornitore_spento_viene_ignorato(solo, monkeypatch) -> None:
    solo(GROQ_API_KEY="k")
    monkeypatch.setattr(config, "pinned_model", lambda task: ("openai", "gpt-4.1"))
    groq = providers.BY_NAME["groq"]
    candidati = [(groq, "llama-3.3-70b-versatile")]

    assert model_selector._pin_first(candidati, "json") == candidati


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


def test_il_niente_da_dire_resta_muto() -> None:
    """L'unico motivo che non si scrive: una ricerca senza risultati si spiega da
    sola, e «consiglio non disponibile» sposterebbe la colpa sull'IA."""
    assert client.worth_saying(client.RATE_LIMITED)
    assert client.worth_saying(client.NOT_CONFIGURED)
    assert not client.worth_saying(client.NOTHING)
    assert not client.worth_saying("")


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
