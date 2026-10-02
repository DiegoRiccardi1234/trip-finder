"""I fornitori di modelli, e l'unica cosa che cambia davvero fra loro.

Il progetto non dipende da un fornitore solo. Il motivo non e' teorico: sul piano
gratuito un `429` non significa quota esaurita, e' il throttle dell'host,
condiviso fra tutti quelli che lo usano in quel momento. Con un solo fornitore
l'IA tace finche' non passa; con tre gratuiti accanto, la richiesta prosegue.

Sei fornitori parlano il formato OpenAI (`POST {base}/chat/completions`) e quindi
condividono tutto il codice: aggiungerne un altro e' una riga in `PROVIDERS`.
Il settimo, Anthropic, parla un formato suo e ha il proprio adattatore in
`anthropic_client.py`; qui compare solo con `dialect="anthropic"`.

Ordine di prova: prima i gratuiti, poi quelli a pagamento. Chi paga vuole
decidere quando spendere, non scoprirlo dalla fattura. `LLM_PROVIDER` nel `.env`
forza un fornitore o un ordine, come negli altri progetti.

Senza nessuna chiave configurata non succede niente di grave: le funzioni che
usano l'IA sono tutte facoltative e il sito funziona identico senza.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import get_settings

#: Modelli di partenza per compito. Sono un punto di partenza, non una verita':
#: i cataloghi cambiano e gli slug muoiono. Chi ha un endpoint per elencare i
#: modelli viene interrogato a runtime (`model_selector.discover`), e questi
#: valgono da rete di sicurezza.
#:
#: **Quanti, e perche' non tre.** Il 2026-08-22 la catena si e' spenta con un
#: pool di quattro: `ling-3.0-flash` ritirato (zero endpoint), i due gemma —
#: gli unici davvero capaci — sotto `429` per ore, e in fondo un solo modello
#: che tronca. Il throttle di un piano gratuito e' **per modello e condiviso**
#: con tutti quelli che lo usano: non e' un evento raro da cui ripararsi, e'
#: la condizione normale di certe ore. La scoperta a runtime non salva da sola,
#: perche' scatta solo quando *nessuno* del pool e' sano, e un modello
#: throttlato per noi resta sanissimo per `/endpoints`. L'unica difesa e' avere
#: dove cadere, quindi la coda tiene modelli di **famiglie diverse**: quando a
#: throttlare e' un host, i suoi li throttla tutti insieme.
#:
#: Quelli in coda non sono stati provati sul compito: ci arrivano solo se i
#: primi non rispondono, e se sbagliano se ne accorge il runtime — troncamento,
#: JSON non valido e 429 diventano penalita' sulla coppia (fornitore, modello).
#: E' la stessa regola scritta in `model_selector`: empirico batte per-nome.
_OPENROUTER_JSON = [
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
]
#: **Tre esclusi il 2026-08-22, e tutti e tre per misura, non per nome.** Sono
#: qui perche' il difetto che li accomuna non si vede dallo slug e il prossimo
#: che allarga il pool rischia di ripescarli:
#:
#: - `inclusionai/ling-3.0-flash:free` — ritirato, `endpoints: []`. Restava in
#:   lista a occupare un posto e a non rispondere mai.
#: - `nvidia/nemotron-3-nano-30b-a3b:free` — il punteggio dal nome lo premiava
#:   (30B, nessun indizio di ragionamento nello slug). Provato due volte sullo
#:   stesso prompt: con i 600 token veri **tronca**; con 2000 arriva in fondo e
#:   risponde `{"stages": [], "date": "", "data_detta": false}` a «da Torino a
#:   Matera. venerdi 28», cioe' butta via una tratta e una data che c'erano
#:   entrambe. Trenta miliardi di parametri con tre attivi restano un modello
#:   da tre.
#: - `nvidia/nemotron-3.5-lightning:free` e `dots-studio/dots-3-note-preview:free`
#:   — aggiunti a questo pool e tolti nella stessa ora: **troncano tutt'e due**
#:   su una risposta che sta in centosessanta caratteri. Il catalogo gratuito
#:   e' ormai fatto quasi solo di modelli che ragionano di default, e il
#:   ragionamento si mangia il budget prima che il JSON cominci. Sul free
#:   resta poco altro che i vecchi `instruct`.
#:
#: E' anche il motivo per cui `max_tokens` **non** si alza per farceli stare: a
#: 600 il troncamento e' un difetto **rumoroso**, che si prende la penalita' e
#: fa scendere il modello in classifica; con piu' spazio diventa una risposta
#: sbagliata e silenziosa, cioe' precisamente cio' che «Interpreta» esiste per
#: evitare. Il numero da alzare non e' il budget: e' il numero di modelli che
#: sanno rispondere senza ragionare ad alta voce.
#: Il consiglio e' testo, non JSON, e la differenza cambia il pool: un modello
#: che ragiona prima di rispondere qui non fa danni, perche' non c'e' nessuna
#: parentesi da chiudere entro il budget.
_OPENROUTER_ADVICE = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-26b-a4b-it:free",
]


class CallFailed(RuntimeError):
    """Una chiamata non ha prodotto niente di utilizzabile.

    Porta con se' il `reason` che il selettore usa come penalita', cosi' il
    chiamante non deve reinterpretare l'eccezione di ciascun fornitore."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass(frozen=True)
class Provider:
    """Un fornitore di modelli. Quello che cambia fra due fornitori compatibili
    OpenAI e' soltanto `base_url` e la chiave: tutto il resto e' condiviso."""

    #: Nome corto, e' anche la chiave con cui si registrano le penalita'.
    name: str
    base_url: str
    #: Nome del campo di `Settings` che porta la chiave.
    key_field: str
    #: I gratuiti si provano per primi.
    free: bool
    #: Pool di partenza per compito ("json", "advice").
    pools: dict[str, list[str]]
    #: "openai" per il formato Chat Completions, "anthropic" per Messages.
    dialect: str = "openai"
    #: Falso solo per il server locale, che non autentica nessuno.
    needs_key: bool = True
    #: Vero se espone `GET {base}/models` per scoprire il catalogo.
    lists_models: bool = True
    #: Note che finiscono in `/api/ai/status`, per capire cosa manca.
    label: str = ""


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        key_field="openrouter_api_key",
        free=True,
        pools={"json": _OPENROUTER_JSON, "advice": _OPENROUTER_ADVICE},
        label="pool gratuito, con controllo di salute per modello",
    ),
    Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        key_field="groq_api_key",
        free=True,
        pools={
            "json": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
            "advice": ["llama-3.3-70b-versatile"],
        },
        label="piano gratuito, molto veloce",
    ),
    Provider(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        key_field="cerebras_api_key",
        free=True,
        pools={
            "json": ["llama-3.3-70b", "llama3.1-8b"],
            "advice": ["llama-3.3-70b"],
        },
        label="piano gratuito, molto veloce",
    ),
    Provider(
        # Google espone un endpoint compatibile OpenAI accanto al suo nativo:
        # cosi' non serve un adattatore dedicato per una quota gratuita.
        name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_field="google_api_key",
        free=True,
        pools={
            # Misurati sul prompt reale il 2026-10-02: JSON completo entro
            # 600 token, ~1.5s Lite / ~2s Flash. I 2.5 restano nel catalogo
            # ma l'API li rifiuta con 404 per le nuove chiavi.
            "json": ["gemini-3.5-flash-lite", "gemini-3.8-flash"],
            "advice": ["gemini-3.8-flash"],
        },
        label="quota gratuita",
    ),
    Provider(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        key_field="mistral_api_key",
        free=True,
        # Pool vuoto di proposito, come per i tre qui sotto: gli slug dei
        # fornitori cambiano piu' in fretta di quanto si aggiorni un file, e
        # `discover_catalog` chiede a loro cosa hanno adesso. Un elenco scritto
        # a mano qui invecchierebbe in silenzio, che e' il difetto peggiore.
        pools={"json": [], "advice": []},
        label="piano gratuito Experiment, senza carta",
    ),
    Provider(
        name="openai",
        base_url="https://api.openai.com/v1",
        key_field="openai_api_key",
        free=False,
        pools={"json": ["gpt-4.1-mini"], "advice": ["gpt-4.1"]},
    ),
    Provider(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        key_field="deepseek_api_key",
        free=False,
        pools={"json": [], "advice": []},
    ),
    Provider(
        name="xai",
        base_url="https://api.x.ai/v1",
        key_field="xai_api_key",
        free=False,
        pools={"json": [], "advice": []},
        label="Grok",
    ),
    Provider(
        name="glm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        key_field="glm_api_key",
        free=False,
        pools={"json": [], "advice": []},
        label="Zhipu GLM",
    ),
    Provider(
        # Un server locale (Ollama, LM Studio) o un gateway. La chiave e'
        # facoltativa perche' in locale non c'e' nessuno da autenticare: il
        # fornitore esiste se e' stato dichiarato un indirizzo.
        name="local",
        base_url="",
        key_field="llm_base_url",
        free=True,
        pools={"json": [], "advice": []},
        needs_key=False,
        label="server locale o gateway, da LLM_BASE_URL",
    ),
    Provider(
        name="anthropic",
        base_url="https://api.anthropic.com",
        key_field="anthropic_api_key",
        free=False,
        # Un solo modello per compito: la scelta fra i Claude la fa l'utente col
        # .env, non un'euristica sul nome che qui non ha appigli.
        pools={"json": ["claude-opus-5"], "advice": ["claude-opus-5"]},
        dialect="anthropic",
        # Il catalogo si elenca, ma con un formato tutto suo: non serve, il pool
        # e' di due voci e non invecchia come quello dei gratuiti.
        lists_models=False,
    ),
)

BY_NAME = {provider.name: provider for provider in PROVIDERS}


def api_key(provider: Provider) -> str:
    return str(getattr(get_settings(), provider.key_field, "") or "")


def base_url(provider: Provider) -> str:
    """L'indirizzo da usare adesso: per il fornitore locale lo decide il `.env`."""
    if provider.name == "local":
        return str(get_settings().llm_base_url or "").rstrip("/")
    return provider.base_url


def is_configured(provider: Provider) -> bool:
    if provider.needs_key:
        return bool(api_key(provider))
    return bool(base_url(provider))


def configured() -> list[Provider]:
    """I fornitori utilizzabili adesso, nell'ordine in cui vanno provati.

    Gratuiti prima, a pagamento dopo. `LLM_PROVIDER` scavalca tutto: accetta un
    nome o un elenco separato da virgole, e i fornitori non nominati restano
    fuori. Un nome sconosciuto viene ignorato invece di far fallire l'avvio, che
    per una funzione facoltativa sarebbe una punizione sproporzionata.

    I fornitori a pagamento restano **fuori** finche' non li si accende con
    `ALLOW_PAID_PROVIDERS`. Metterli in fondo alla fila non bastava: una
    giornata storta dei gratuiti li faceva scattare lo stesso, e la spesa si
    scopriva dopo."""
    settings = get_settings()
    available = [
        provider
        for provider in PROVIDERS
        if is_configured(provider) and (provider.free or settings.allow_paid_providers)
    ]

    wanted = [name.strip() for name in str(settings.llm_provider or "").split(",")]
    wanted = [name for name in wanted if name]
    if wanted:
        # Nominarlo e' gia' una scelta esplicita: se lo chiedi per nome lo hai,
        # anche se si paga. L'interruttore difende dalla spesa non voluta, non
        # da quella voluta.
        chosen = [BY_NAME[name] for name in wanted if name in BY_NAME]
        return [provider for provider in chosen if is_configured(provider)]

    available.sort(key=lambda provider: (not provider.free, provider.name))
    return available


def pool_for(provider: Provider, task: str) -> list[str]:
    """Pool di partenza per un compito.

    `OPENROUTER_JSON_MODELS` e `OPENROUTER_ADVICE_MODELS` restano l'unico
    override esplicito: sono i pool che invecchiano davvero, perche' il piano
    gratuito ruota. Per gli altri fornitori il pool scritto qui e' solo un punto
    di partenza, e chi elenca il proprio catalogo viene comunque interrogato a
    runtime."""
    if provider.name == "openrouter":
        override = get_settings().model_pool(task)
        if override:
            return override
    return list(provider.pools.get(task, ()))
