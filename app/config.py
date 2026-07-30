"""Configurazione globale, letta da .env con override da ambiente."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FIXTURES_DIR = ROOT / "tests" / "fixtures"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- IA ---
    openrouter_api_key: str = ""
    openrouter_json_models: str = ""
    openrouter_advice_models: str = ""

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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.cookies_dir.mkdir(parents=True, exist_ok=True)
    return settings
