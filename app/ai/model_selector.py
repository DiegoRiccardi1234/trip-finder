"""Scelta del modello in base al compito, non "il piu' grosso disponibile".

Due compiti molto diversi convivono in questo progetto:

  - **JSON ad alto volume** (interpretare una richiesta, proporre scali). Serve
    un modello veloce e ordinato di taglia media. I modelli con ragionamento
    esteso sono la scelta peggiore: bruciano il budget di token in una catena
    di pensiero nascosta e restituiscono JSON troncato. Il segnale da guardare
    e' `finish_reason`: se vale "length" il completamento e' stato tagliato e
    il JSON non e' affidabile nemmeno quando per caso si riesce a interpretarlo.
  - **Testo di sintesi** (il consiglio finale). Qui serve capacita', si accetta
    di essere piu' lenti.

Taglia e qualita' non coincidono. Un 27B pulito batte un 235B con ragionamento
che tronca. La soglia di qualita' sta intorno ai 24-27 miliardi di parametri:
sotto si scende nei modelli giocattolo, sopra si escludono i modelli medi
puliti e restano i giganti che troncano.

Il nome non dice se un modello ragiona: `nemotron-super`, `gpt-oss`, `hy3` non
hanno "reasoning" nello slug. Per questo alla fine conta l'evidenza raccolta a
runtime, non l'euristica sul nome.
"""

from __future__ import annotations

import logging
import re
import time

from app.ai import endpoint_health
from app.config import get_settings
from app.orchestrator import cache
from app.orchestrator.db import get_db
from app.providers.http_client import HttpError, get_http_client

logger = logging.getLogger(__name__)

#: Pool di partenza, verificati a luglio 2026. Il piano gratuito di OpenRouter
#: ruota in fretta: modelli che c'erano due mesi fa rispondono `endpoints: []`.
#: Per questo il pool e' solo un punto di partenza; se muore tutto si passa alla
#: scoperta automatica piu' sotto, e .env permette comunque di imporne altri.
DEFAULT_POOLS: dict[str, list[str]] = {
    "json": [
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "inclusionai/ling-3.0-flash:free",
    ],
    "advice": [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "inclusionai/ling-3.0-flash:free",
        "google/gemma-4-26b-a4b-it:free",
    ],
}

MODELS_URL = "https://openrouter.ai/api/v1/models"

#: Slug da non proporre mai automaticamente: non sono modelli conversazionali
#: o hanno difetti noti su questi compiti.
DISCOVERY_BLOCKLIST = ("lyria", "content-safety", "-vl", "-code", "whisper", "embed")

#: Sotto questa taglia i modelli sbagliano struttura e istruzioni troppo spesso.
QUALITY_FLOOR_B = 24.0

SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b")
REASONING_HINTS = ("thinking", "reason", "-r1", "qwq", "deepthink", "-cot")
INSTRUCT_HINTS = ("instruct", "-it", "chat", "flash")

#: Quanto dura una penalita' raccolta sul campo.
PENALTY_TTL = 30 * 60
PENALTY_WEIGHT = {
    "truncated": 6.0,  # JSON tagliato: il difetto piu' grave per questi usi
    "empty": 4.0,
    "rate_limited": 2.0,
    "error": 3.0,
}


def parse_size_b(slug: str) -> float | None:
    """Miliardi di parametri desunti dallo slug, quando dichiarati."""
    matches = SIZE_RE.findall(slug.lower())
    if not matches:
        return None
    # "llama-4-maverick-17b-128e": vince il numero piu' grande, che e' la taglia.
    return max(float(value) for value in matches)


def score_model_name(slug: str, task: str) -> float:
    """Punteggio a priori, dal solo nome. Grezzo ma sorprendentemente utile."""
    lower = slug.lower()
    score = 0.0

    size = parse_size_b(lower)
    if size is not None:
        if size < QUALITY_FLOOR_B:
            score -= 4.0  # sotto la soglia di qualita'
        elif task == "json":
            # Per il JSON la taglia in eccesso non aiuta e costa latenza.
            score += 2.0 if size <= 80 else 0.5
        else:
            score += 1.0 + min(2.0, size / 100.0)

    if any(hint in lower for hint in INSTRUCT_HINTS):
        score += 1.5
    if any(hint in lower for hint in REASONING_HINTS):
        # Sul JSON e' quasi sempre un problema; sul testo lungo molto meno.
        score -= 3.0 if task == "json" else 0.5
    if lower.endswith(":free"):
        score += 0.5

    return score


async def record_penalty(model: str, reason: str, provider: str = "openrouter") -> None:
    """Registra un difetto osservato davvero, per la durata del compito.

    La penalita' e' per coppia (provider, modello): lo stesso slug puo' troncare
    su un host e funzionare su un altro, e spegnerlo ovunque per colpa di uno
    solo sarebbe uno spreco."""
    db = await get_db()
    weight = PENALTY_WEIGHT.get(reason, 2.0)
    await db.execute(
        "INSERT INTO model_penalty(provider, model, reason, penalty, expires_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, model) DO UPDATE SET "
        "reason = excluded.reason, penalty = model_penalty.penalty + excluded.penalty, "
        "expires_at = excluded.expires_at",
        (provider, model, reason, weight, time.time() + PENALTY_TTL),
    )
    await db.commit()
    logger.info("penalita' %s per %s (%s)", weight, model, reason)


async def current_penalties(provider: str = "openrouter") -> dict[str, float]:
    db = await get_db()
    async with db.execute(
        "SELECT model, penalty FROM model_penalty WHERE provider = ? AND expires_at > ?",
        (provider, time.time()),
    ) as cursor:
        rows = await cursor.fetchall()
    return {model: penalty for model, penalty in rows}


async def clear_penalties() -> None:
    db = await get_db()
    await db.execute("DELETE FROM model_penalty")
    await db.commit()


async def discover_free_models(limit: int = 8) -> list[str]:
    """Modelli gratuiti realmente in catalogo adesso, i piu' adatti per primi.

    Serve quando il pool scritto nel codice e' invecchiato. E' successo gia' una
    volta: cinque slug su cinque rispondevano `endpoints: []` perche' nel
    frattempo erano stati ritirati. Senza questa rete di sicurezza l'IA sarebbe
    semplicemente sparita, e in modo silenzioso."""
    key = cache.make_key("openrouter:free_models")
    cached = await cache.get(key)
    if cached is not None:
        return list(cached)[:limit]

    try:
        payload = await get_http_client().get_json(
            MODELS_URL, headers={"Accept": "application/json"}, retries=1
        )
    except HttpError as exc:
        logger.debug("catalogo modelli non raggiungibile: %s", exc)
        return []

    free: list[str] = []
    for model in (payload.get("data") if isinstance(payload, dict) else None) or []:
        slug = model.get("id") if isinstance(model, dict) else None
        pricing = model.get("pricing") if isinstance(model, dict) else None
        if not slug or not isinstance(pricing, dict):
            continue
        try:
            if float(pricing.get("prompt", 1)) or float(pricing.get("completion", 1)):
                continue
        except (TypeError, ValueError):
            continue
        if any(word in slug.lower() for word in DISCOVERY_BLOCKLIST):
            continue
        free.append(slug)

    await cache.set(key, free, kind="static", ttl=6 * 3600)
    return free[:limit]


async def rank_models(task: str, candidates: list[str] | None = None) -> list[str]:
    """Modelli da provare in ordine, i migliori per primi.

    L'ordine finale nasce da tre segnali, in questa gerarchia: la salute live
    (un modello morto non ha appello), la fascia di disponibilita' e infine la
    qualita' attesa per quel compito. La fascia serve proprio a impedire che
    mezzo punto di uptime scavalchi un modello nettamente piu' adatto."""
    settings = get_settings()
    pool = candidates or settings.model_pool(task) or DEFAULT_POOLS.get(task, [])
    if not pool:
        return []

    order = await _rank_pool(pool, task)
    if order:
        return order

    # Pool invecchiato: si ricostruisce dal catalogo vivo invece di arrendersi.
    logger.warning("nessun modello sano nel pool %s, passo alla scoperta", task)
    discovered = [slug for slug in await discover_free_models(limit=10) if slug not in pool]
    order = await _rank_pool(discovered, task)
    if order:
        logger.info("pool %s ricostruito dal catalogo: %s", task, order[:3])
        return order

    # Ultima risorsa: si prova comunque la lista originale. Meglio un tentativo
    # che rinunciare in silenzio.
    return pool


async def _rank_pool(pool: list[str], task: str) -> list[str]:
    if not pool:
        return []

    health = await endpoint_health.check_many(pool)
    penalties = await current_penalties()

    alive = [
        (slug, health.get(slug, endpoint_health.UNKNOWN))
        for slug in pool
        if health.get(slug, endpoint_health.UNKNOWN).alive
    ]
    for slug in pool:
        status = health.get(slug, endpoint_health.UNKNOWN)
        if not status.alive:
            logger.debug("scartato %s: %s", slug, status.detail)
    if not alive:
        return []

    tiers = endpoint_health.tiers([status.uptime_5m for _, status in alive])
    ranked = [
        (tier, score_model_name(slug, task) - penalties.get(slug, 0.0), slug)
        for (slug, _), tier in zip(alive, tiers)
    ]
    # Prima la fascia di disponibilita' (0 = migliore), poi la qualita' attesa
    # dentro la fascia: cosi' un modello piu' adatto non viene scavalcato per
    # due decimi di punto percentuale.
    ranked.sort(key=lambda item: (item[0], -item[1]))
    return [slug for _, _, slug in ranked]
