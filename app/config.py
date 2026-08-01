"""Configurazione globale: `.env`, ambiente, e quello che l'utente salva dal sito.

Le chiavi dei fornitori arrivano da due posti. Il `.env` e' per chi lavora al
codice; chi usa l'applicazione non ha un terminale e le mette dalle
impostazioni, e finiscono in `data/local_secrets.json` — fuori dal repository,
come il database. Fra i due vince il file: chi ha appena premuto Salva si
aspetta che valga adesso, non al prossimo riavvio.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _workspace() -> Path:
    """La cartella dove vivono `.env`, `data/`, il database e i profili browser.

    Da sorgente e' la radice del progetto. Dentro l'eseguibile no: li'
    `__file__` sta in `_MEIPASS`, una cartella temporanea di **sola lettura**
    che PyInstaller cancella alla chiusura, e scriverci il database vorrebbe
    dire perderlo a ogni avvio. Il lanciatore dichiara il workspace vero — la
    cartella accanto all'eseguibile — prima di importare l'applicazione."""
    dichiarato = os.environ.get("TRIPFINDER_WORKSPACE")
    if dichiarato:
        return Path(dichiarato).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _workspace()
DATA_DIR = ROOT / "data"
#: Le fixture sono materiale del progetto, non dell'utente: da sorgente stanno
#: accanto ai test e nel bundle non ci sono affatto.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: Le chiavi salvate dall'interfaccia. Vive accanto al database e non entra nel
#: repository: e' un segreto dell'utente, non del progetto.
LOCAL_SECRETS_FILE = "local_secrets.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # I valori salvati dal sito si applicano per assegnazione: senza
        # validazione un `"true"` scritto a mano nel JSON resterebbe una stringa
        # e `allow_paid_providers` sarebbe vero per il solo fatto di non essere
        # vuoto — il tipo di errore che non da' errore.
        validate_assignment=True,
    )

    # --- IA ---
    # Una chiave qualsiasi basta: i fornitori configurati si provano in catena,
    # gratuiti per primi (app/ai/providers.py). Senza nessuna chiave il sito
    # funziona identico, solo senza consigli e senza linguaggio naturale.
    openrouter_api_key: str = ""
    groq_api_key: str = ""
    cerebras_api_key: str = ""
    google_api_key: str = ""
    mistral_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    xai_api_key: str = ""
    glm_api_key: str = ""
    #: I fornitori a pagamento restano fuori finche' non li si accende qui. Non
    #: basta metterli in fondo alla lista: una giornata storta dei gratuiti li
    #: farebbe scattare comunque, e chi paga vuole deciderlo lui.
    allow_paid_providers: bool = False
    #: Un server locale compatibile OpenAI (Ollama, LM Studio) o un gateway.
    llm_base_url: str = ""
    #: Forza un fornitore o un ordine ("groq" oppure "groq,openrouter").
    llm_provider: str = ""
    #: I modelli hanno tempi loro. `http_timeout` e' tarato sulle API dei
    #: vettori, che rispondono in un paio di secondi; un modello che genera
    #: mille token su un endpoint gratuito supera i venti secondi di regola, e
    #: con quel tetto il consiglio moriva per timeout senza che si vedesse.
    llm_timeout: float = 45.0
    openrouter_json_models: str = ""
    openrouter_advice_models: str = ""
    #: Modello scelto a mano per un compito, nella forma `fornitore/modello`
    #: (`openrouter/google/gemma-4-31b-it:free`). Vuoto = lo decide il ranking.
    #: Non e' esclusivo: va in testa alla fila, e se rifiuta la richiesta
    #: prosegue sugli altri — che e' il motivo per cui esiste la fila.
    json_model: str = ""
    advice_model: str = ""

    # --- ricerca ---
    # --- ricerca in due ondate ---
    # Gli operatori si dividono in due mondi. Quelli interrogabili via HTTP
    # rispondono in un paio di secondi; quelli che richiedono un browser ne
    # costano dieci o venti. Con un budget unico o si taglia fuori il secondo
    # gruppo o si fa aspettare tutti al ritmo del piu' lento. Con due budget
    # l'utente vede i primi risultati subito e gli altri si aggiungono dopo.
    fast_budget: float = 20.0
    search_time_budget: float = 90.0
    #: Richieste in volo in tutta la ricerca. Puo' essere alta perche' il limite
    #: che protegge i singoli operatori e' un altro, per dominio: con trenta
    #: adapter su trenta host diversi non si contendono niente, e tenere basso
    #: questo numero li mette in fila per nulla.
    search_max_concurrency: int = 20
    #: Richieste al secondo verso lo stesso dominio. Sono API JSON, non pagine
    #: da scrapare: due-tre al secondo restano un carico trascurabile per loro
    #: e sono il vero collo di bottiglia della ricerca.
    domain_rate_limit: float = 4.0

    # --- scraping ---
    impersonate_profile: str = "chrome"
    browser_headless: bool = True
    http_timeout: float = 20.0

    # --- risoluzione geografica ---
    # Raggio entro cui una fermata di terra e' considerata "a" quella localita'.
    ground_radius_km: float = 30.0
    # Gli aeroporti servono un bacino molto piu' ampio.
    air_radius_km: float = 120.0
    # I porti, al contrario, hanno bisogno di un raggio stretto: quindici
    # chilometri di mare non sono quindici chilometri di strada. Con il raggio
    # normale, il porto di Levanzo finiva fra quelli di Trapani e il motore
    # proponeva di raggiungere un'isola in autobus.
    port_radius_km: float = 12.0

    # --- composizione ---
    max_hubs_per_itinerary: int = 2
    max_candidate_paths: int = 8
    #: Percorsi visibili agli adapter lenti. Dieci adapter browser per tredici
    #: tratte sarebbero centotrenta sessioni: qui si limitano al diretto e ai
    #: due scali migliori, dove le soluzioni vere si trovano comunque.
    max_paths_for_slow_providers: int = 3
    # Un itinerario via hub non puo' essere piu' lungo di questo fattore
    # rispetto alla distanza in linea d'aria origine-destinazione.
    max_detour_factor: float = 1.4

    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "trip_finder.db"

    @property
    def cookies_dir(self) -> Path:
        return DATA_DIR / "cookies"

    def model_pool(self, kind: str) -> list[str]:
        """Pool di modelli configurato dall'utente per un tipo di task, se presente."""
        raw = {
            "json": self.openrouter_json_models,
            "advice": self.openrouter_advice_models,
        }.get(kind, "")
        return [m.strip() for m in raw.split(",") if m.strip()]


#: I campi che l'interfaccia puo' scrivere. Un elenco esplicito e non "tutto
#: quello che c'e' nel file": un JSON malformato o vecchio non deve poter
#: cambiare la concorrenza della ricerca o il livello di log.
SETTABLE_FIELDS = (
    "openrouter_api_key",
    "groq_api_key",
    "cerebras_api_key",
    "google_api_key",
    "mistral_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "deepseek_api_key",
    "xai_api_key",
    "glm_api_key",
    "llm_base_url",
    "llm_provider",
    "allow_paid_providers",
    "json_model",
    "advice_model",
)


def pinned_model(task: str) -> tuple[str, str] | None:
    """La coppia (fornitore, modello) scelta a mano per un compito, se c'e'.

    Lo slug di un modello contiene barre (`google/gemma-4-31b-it:free`), quindi
    si divide **solo alla prima**: il resto e' il modello."""
    raw = str(getattr(get_settings(), f"{task}_model", "") or "").strip()
    if "/" not in raw:
        return None
    provider, _, model = raw.partition("/")
    return (provider, model) if provider and model else None


def secrets_path() -> Path:
    return DATA_DIR / LOCAL_SECRETS_FILE


def read_local_secrets() -> dict[str, Any]:
    path = secrets_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Un file rotto non deve impedire l'avvio: si riparte dal `.env` e lo si
        # dice nel log, invece di lasciare il sito morto senza spiegazione.
        logger.warning("%s illeggibile (%s): uso solo .env", path.name, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_local_secrets(values: dict[str, Any]) -> dict[str, Any]:
    """Scrive i campi indicati e restituisce il contenuto aggiornato.

    Convenzione, la stessa di Job Finder: una stringa non vuota imposta, una
    stringa vuota **cancella** (il bottone «Rimuovi»), un campo assente resta
    com'e'. Cosi' la stessa chiamata serve a scrivere una chiave sola senza
    doversi portare dietro tutte le altre."""
    current = read_local_secrets()
    for field, raw in values.items():
        if field not in SETTABLE_FIELDS:
            continue
        if isinstance(raw, bool):
            current[field] = raw
            continue
        value = str(raw or "").strip()
        if value:
            current[field] = value
        else:
            current.pop(field, None)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    secrets_path().write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return current


def _apply_local_secrets(settings: Settings) -> None:
    for field, value in read_local_secrets().items():
        if field not in SETTABLE_FIELDS:
            continue
        try:
            setattr(settings, field, value)
        except (TypeError, ValueError) as exc:  # tipo sbagliato nel file
            logger.warning("valore ignorato per %s: %s", field, exc)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.cookies_dir.mkdir(parents=True, exist_ok=True)
    # Dopo il `.env`, non prima: quello che l'utente ha salvato dal sito vince.
    _apply_local_secrets(settings)
    return settings
