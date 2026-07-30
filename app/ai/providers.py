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
_OPENROUTER_JSON = [
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "inclusionai/ling-3.0-flash:free",
]
_OPENROUTER_ADVICE = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash:free",
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
            "json": ["gemini-2.5-flash-lite", "gemini-2.5-flash"],
            "advice": ["gemini-2.5-flash"],
        },
        label="quota gratuita",
    ),
    Provider(
        name="openai",
        base_url="https://api.openai.com/v1",
        key_field="openai_api_key",
        free=False,
        pools={"json": ["gpt-4.1-mini"], "advice": ["gpt-4.1"]},
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
    per una funzione facoltativa sarebbe una punizione sproporzionata."""
    available = [provider for provider in PROVIDERS if is_configured(provider)]

    wanted = [name.strip() for name in str(get_settings().llm_provider or "").split(",")]
    wanted = [name for name in wanted if name]
    if wanted:
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
