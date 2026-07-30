"""Chiamate ai modelli con failover fra fornitori e apprendimento dai difetti.

Regole che nascono dall'uso, quasi tutte dal piano gratuito:

  - Un 429 su un modello gratuito quasi mai significa quota esaurita: e' il
    throttle dell'host, condiviso fra tutti gli utenti. Si passa al modello
    successivo, e se il fornitore e' esaurito si passa al fornitore successivo:
    e' il motivo per cui questo modulo gira su coppie (fornitore, modello) e non
    su un elenco di modelli.
  - `finish_reason == "length"` va trattato come fallimento anche se il testo
    sembra completo. Su una risposta JSON e' il caso peggiore: l'oggetto esterno
    puo' risultare chiuso mentre un array dentro e' tagliato, e
    l'interpretazione riesce restituendo dati incompleti senza segnalare nulla.
  - Ogni difetto osservato diventa una penalita' per **quella coppia**, non per
    il modello: lo stesso slug puo' troncare su un host e funzionare su un
    altro, e spegnerlo ovunque per colpa di uno solo sarebbe uno spreco.

Se non c'e' nessuna chiave, o se tutti i tentativi falliscono, si restituisce
`None`: le funzioni che usano l'IA sono tutte facoltative e il sito funziona
senza.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import orjson

from app.ai import model_selector, providers
from app.ai.providers import CallFailed, Provider
from app.orchestrator import cache
from app.providers.http_client import Blocked, HttpError, get_http_client

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4


class AIUnavailable(RuntimeError):
    """Nessun modello ha risposto in modo utilizzabile."""


@dataclass
class Completion:
    text: str
    model: str
    finish_reason: str | None
    provider: str = "openrouter"


def is_configured() -> bool:
    """Vero se almeno un fornitore e' utilizzabile."""
    return bool(providers.configured())


async def _call_openai_dialect(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
    json_mode: bool,
) -> tuple[str, str | None]:
    """Il formato Chat Completions, che sei fornitori su sette parlano uguale."""
    base = providers.base_url(provider)
    if not base:
        raise CallFailed("error", "indirizzo non configurato")

    headers = {"Content-Type": "application/json"}
    key = providers.api_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider.name == "openrouter":
        # Facoltativi: OpenRouter li usa per attribuire il traffico.
        headers["HTTP-Referer"] = "http://localhost:8010/"
        headers["X-Title"] = "Trip Finder"

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
        data = await get_http_client().post_json(
            f"{base}/chat/completions", json=payload, headers=headers, retries=0
        )
    except Blocked as exc:
        raise CallFailed("rate_limited", str(exc)) from exc
    except HttpError as exc:
        raise CallFailed("error", str(exc)) from exc

    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    return text, choice.get("finish_reason")


async def _call(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
    json_mode: bool,
    task: str,
) -> tuple[str, str | None]:
    if provider.dialect == "anthropic":
        from app.ai import anthropic_client

        return await anthropic_client.call(
            provider, model, system, user, max_tokens=max_tokens, task=task
        )
    return await _call_openai_dialect(
        provider,
        model,
        system,
        user,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
    )


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
    """Prova le coppie (fornitore, modello) in ordine finche' una risponde bene."""
    if not is_configured():
        return None

    if cache_key:
        cached = await cache.get(cache_key)
        if cached:
            # Le voci scritte prima del multi-fornitore non hanno il campo:
            # vale la pena leggerle lo stesso invece di buttare la cache.
            return Completion(
                text=cached.get("text", ""),
                model=cached.get("model", ""),
                finish_reason=cached.get("finish_reason"),
                provider=cached.get("provider", "openrouter"),
            )

    candidates = await model_selector.rank_candidates(task)
    if not candidates:
        return None

    for provider, model in candidates[:MAX_ATTEMPTS]:
        try:
            text, finish = await _call(
                provider,
                model,
                system,
                user,
                max_tokens=max_tokens,
                temperature=temperature,
                json_mode=json_mode,
                task=task,
            )
        except CallFailed as exc:
            await model_selector.record_penalty(model, exc.reason, provider=provider.name)
            # Il motivo da solo non basta a capire cosa e' successo: "error" puo'
            # essere un 401, un 500 o un timeout, e senza il dettaglio si resta a
            # indovinare proprio quando serve saperlo.
            logger.info(
                "%s/%s non utilizzabile (%s: %s), passo al successivo",
                provider.name, model, exc.reason, exc,
            )
            continue

        if finish == "length":
            # Il difetto piu' insidioso: la risposta esiste ma e' tagliata.
            await model_selector.record_penalty(model, "truncated", provider=provider.name)
            logger.info("%s/%s ha troncato la risposta", provider.name, model)
            continue
        if not text:
            await model_selector.record_penalty(model, "empty", provider=provider.name)
            continue

        completion = Completion(
            text=text, model=model, finish_reason=finish, provider=provider.name
        )
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
        await model_selector.record_penalty(
            completion.model, "error", provider=completion.provider
        )
        logger.info("%s/%s non ha prodotto JSON valido", completion.provider, completion.model)
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
