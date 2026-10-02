"""Salute live degli endpoint OpenRouter, prima di scegliere un modello.

Sul piano gratuito i modelli spariscono, si degradano o restano in lista pur
non avendo piu' nessun provider dietro. Interrogarli e' tempo perso e, peggio,
consuma tentativi di una quota condivisa.

`GET /api/v1/models/{slug}/endpoints` risponde **senza autenticazione**, non
esegue inferenza e quindi non intacca la quota giornaliera. Costa una richiesta
HTTP e dice tutto quello che serve:

  - `endpoints: []`            -> modello morto, nessun provider lo serve piu'
  - `status`                   -> 0 significa operativo
  - `uptime_last_5m`           -> affidabilita' nell'immediato

Se la verifica fallisce non si blocca nulla: si restituisce la lista dei
candidati invariata e si lascia decidere al failover a valle.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

from app.orchestrator import cache
from app.providers.http_client import HttpError, get_http_client

logger = logging.getLogger(__name__)

ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"

#: Sotto questa disponibilita' nei cinque minuti precedenti il modello e' da
#: evitare: risponderebbe a intermittenza e ogni fallimento costa un giro di
#: failover.
MIN_UPTIME_5M = 80.0

#: Ampiezza della fascia di merito. Due modelli entro due punti di uptime sono
#: equivalenti nei fatti: a quel punto deve vincere la qualita', non un decimale
#: di disponibilita', altrimenti un modello valido viene scavalcato per niente.
#: La fascia si calcola **rispetto al migliore del gruppo**, non su valori
#: assoluti: con soglie fisse un 99,8% e un 100% finirebbero in fasce diverse
#: solo perche' cade li' il confine, che e' esattamente il difetto da evitare.
UPTIME_BUCKET = 2.0


def tiers(values: list[float]) -> list[int]:
    """Fascia di ciascun valore rispetto al massimo: 0 e' la fascia migliore."""
    if not values:
        return []
    best = max(values)
    return [int((best - value) // UPTIME_BUCKET) for value in values]

#: Quante verifiche in parallelo. Serve un limite: sono richieste gratuite ma
#: restano richieste.
MAX_CONCURRENT_CHECKS = 6


@dataclass(frozen=True)
class Health:
    slug: str
    alive: bool
    uptime_5m: float
    uptime_1d: float
    providers: tuple[str, ...] = ()
    detail: str = ""


UNKNOWN = Health(slug="", alive=True, uptime_5m=0.0, uptime_1d=0.0, detail="non verificato")


def _parse(slug: str, payload: object) -> Health:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None

    if not isinstance(endpoints, list):
        return Health(slug, True, 0.0, 0.0, detail="non verificato: risposta incompleta")
    if not endpoints:
        return Health(slug, False, 0.0, 0.0, detail="nessun provider serve piu' il modello")

    healthy = [
        endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict) and type(endpoint.get("status")) is int and endpoint["status"] == 0
    ]
    if not healthy:
        if any(not isinstance(e, dict) or type(e.get("status")) is not int for e in endpoints):
            return Health(slug, True, 0.0, 0.0, detail="non verificato: stato endpoint assente")
        return Health(slug, False, 0.0, 0.0, detail="tutti gli endpoint sono in errore")

    def uptime(endpoint: dict, key: str) -> float | None:
        value = endpoint.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and 0 <= value <= 100:
                return float(value)
        return None

    # Conta il provider migliore: OpenRouter instrada li' per primo.
    measurements = [uptime(endpoint, "uptime_last_5m") for endpoint in healthy]
    best_5m = max((v for v in measurements if v is not None), default=0.0)
    best_1d = max((uptime(e, "uptime_last_1d") or 0.0 for e in healthy), default=0.0)
    providers = tuple(
        str(endpoint.get("provider_name"))
        for endpoint in healthy
        if endpoint.get("provider_name")
    )

    if best_5m < MIN_UPTIME_5M:
        if None in measurements or any(
            not isinstance(e, dict) or type(e.get("status")) is not int for e in endpoints
        ):
            return Health(slug, True, 0.0, best_1d, providers,
                          detail="non verificato: disponibilita' endpoint assente")
        return Health(
            slug, False, best_5m, best_1d, providers,
            detail=f"disponibilita' al {best_5m:.0f}% negli ultimi 5 minuti",
        )

    return Health(slug, True, best_5m, best_1d, providers)


async def check(slug: str) -> Health:
    """Salute di un singolo modello, con cache breve."""
    key = cache.make_key("model_health", slug)
    cached = await cache.get(key)
    if cached is not None:
        return Health(**cached)

    try:
        payload = await get_http_client().get_json(
            ENDPOINTS_URL.format(slug=slug),
            headers={"Accept": "application/json"},
            retries=1,
        )
    except HttpError as exc:
        # Nessuna informazione non e' una bocciatura: si lascia il modello in
        # gioco e sara' il failover a scartarlo se davvero non risponde.
        logger.debug("salute di %s non verificabile: %s", slug, exc)
        return Health(slug, True, 0.0, 0.0, detail="verifica non riuscita")

    health = _parse(slug, payload)
    await cache.set(key, health.__dict__, kind="model_health")
    return health


async def check_many(slugs: list[str]) -> dict[str, Health]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def one(slug: str) -> tuple[str, Health]:
        async with semaphore:
            return slug, await check(slug)

    results = await asyncio.gather(*(one(slug) for slug in slugs), return_exceptions=True)
    health: dict[str, Health] = {}
    for result in results:
        if isinstance(result, BaseException):
            continue
        slug, value = result
        health[slug] = value
    return health
