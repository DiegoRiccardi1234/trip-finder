"""Chiamate a OpenRouter con failover a catena e apprendimento dai difetti.

Regole che nascono dall'uso del piano gratuito:

  - Un 429 su un modello gratuito quasi mai significa quota esaurita: e' il
    throttle dell'host, condiviso fra tutti gli utenti. Si passa al modello
    successivo, non si abbandona la richiesta.
  - `finish_reason == "length"` va trattato come fallimento anche se il testo
    sembra completo. Su una risposta JSON e' il caso peggiore: l'oggetto esterno
    puo' risultare chiuso mentre un array dentro e' tagliato, e l'interpretazione
    riesce restituendo dati incompleti senza segnalare nulla.
  - Ogni difetto osservato diventa una penalita' per quel modello, cosi' i
    tentativi successivi dello stesso compito non ci ricascano.

Se manca la chiave, o se tutti i modelli falliscono, si restituisce `None`: le
funzioni che usano l'IA sono tutte facoltative e il sito funziona senza.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import orjson

from app.ai import model_selector
from app.config import get_settings
from app.orchestrator import cache
from app.providers.http_client import Blocked, HttpError, get_http_client

logger = logging.getLogger(__name__)

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_ATTEMPTS = 4


class AIUnavailable(RuntimeError):
    """Nessun modello ha risposto in modo utilizzabile."""


@dataclass
class Completion:
    text: str
    model: str
    finish_reason: str | None


def is_configured() -> bool:
    return bool(get_settings().openrouter_api_key)


async def complete(
    task: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 900,
    temperature: float = 0.2,
    json_mode: bool = False,
    cache_key: str | None = None,
) -> Completion | None:
    """Prova i modelli in ordine finche' uno risponde in modo utilizzabile."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        return None

    if cache_key:
        cached = await cache.get(cache_key)
        if cached:
            return Completion(**cached)

    models = await model_selector.rank_models(task)
    if not models:
        return None

    http = get_http_client()
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        # OpenRouter li usa per attribuire il traffico; sono facoltativi.
        "HTTP-Referer": "http://localhost:8000/",
        "X-Title": "Trip Finder",
    }

    for model in models[:MAX_ATTEMPTS]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            data = await http.post_json(CHAT_URL, json=payload, headers=headers, retries=0)
        except Blocked:
            await model_selector.record_penalty(model, "rate_limited")
            logger.info("%s limitato a monte, passo al successivo", model)
            continue
        except HttpError as exc:
            await model_selector.record_penalty(model, "error")
            logger.info("%s in errore (%s), passo al successivo", model, exc)
            continue

        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason")

        if finish == "length":
            # Il difetto piu' insidioso: la risposta esiste ma e' tagliata.
            await model_selector.record_penalty(model, "truncated")
            logger.info("%s ha troncato la risposta, passo al successivo", model)
            continue
        if not text:
            await model_selector.record_penalty(model, "empty")
            continue

        completion = Completion(text=text, model=model, finish_reason=finish)
        if cache_key:
            await cache.set(cache_key, completion.__dict__, kind="ai")
        return completion

    logger.warning("nessun modello utilizzabile per il compito %s", task)
    return None


async def complete_json(
    task: str, system: str, user: str, *, max_tokens: int = 700
) -> dict | None:
    """Come `complete`, ma restituisce l'oggetto gia' interpretato."""
    completion = await complete(
        task, system, user, max_tokens=max_tokens, temperature=0.0, json_mode=True
    )
    if completion is None:
        return None

    parsed = _extract_json(completion.text)
    if parsed is None:
        await model_selector.record_penalty(completion.model, "error")
        logger.info("%s non ha prodotto JSON valido", completion.model)
        return None
    return parsed


def _extract_json(text: str) -> dict | None:
    """Recupera l'oggetto anche quando arriva dentro un blocco di codice."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1] if "```" in candidate[3:] else candidate[3:]
        candidate = candidate.removeprefix("json").strip()

    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = orjson.loads(candidate[start : end + 1])
    except orjson.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
