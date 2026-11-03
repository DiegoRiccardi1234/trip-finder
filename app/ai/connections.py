"""Task-specific AI routes in the application's existing SQLite store.

Native account credentials stay in the official clients. An explicit route is
terminal: failures never reopen the legacy provider pool. Only unassigned tasks
retain the old API selection behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from app import config
from app.ai import runtime
from app.ai.providers import CallFailed
from app.ai.runtime_types import TruncatedCompletionError, validate_output
from app.orchestrator.db import get_db

TASKS = {
    "parse": "Interpretazione del viaggio",
    "planning": "Scali e pianificazione",
    "advice": "Consigli e confronti",
    "chat": "Chat viaggi",
}
ALIASES = {"json": "parse"}
JSON_SCHEMAS = {
    "parse": {
        "type": "object", "required": ["stages", "date", "data_detta"],
        "properties": {
            "stages": {"type": "array", "maxItems": 8, "items": {
                "type": "object", "required": ["origin", "destination"],
                "properties": {
                    "origin": {"type": "string"}, "destination": {"type": "string"},
                    "stay_days": {"type": "integer", "minimum": 0, "maximum": 365},
                    "arrive_by": {"type": ["string", "null"], "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                    "depart_after": {"type": ["string", "null"], "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                },
            }},
            "date": {"type": "string", "format": "date"}, "data_detta": {"type": "boolean"},
            "pax": {"type": "integer", "minimum": 1, "maximum": 9},
            "modes": {"type": "array", "items": {"enum": ["rail", "bus", "air", "ferry"]}},
            "with_checked_bag": {"type": "boolean"},
            "allow_night": {"type": "boolean"},
            "max_budget": {"type": ["number", "null"], "minimum": 0},
            "max_changes": {"type": ["integer", "null"], "minimum": 0, "maximum": 5},
        },
    },
    "planning": {"type": "object", "required": ["hubs"], "additionalProperties": False,
                 "properties": {"hubs": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 150}}}},
}
_rows: dict[str, dict[str, Any]] = {}
_routes: dict[str, dict[str, Any]] = {}
_store_key = ""
_guard: asyncio.Lock | None = None
_guard_loop: asyncio.AbstractEventLoop | None = None
_active: set[runtime.NativeRuntimeProvider] = set()
_active_jobs: set[asyncio.Task] = set()


def task_name(task: str) -> str:
    value = ALIASES.get(task, task)
    if value not in TASKS:
        raise ValueError("invalid_routing_task")
    return value


def legacy_task(task: str) -> str:
    return "json" if task_name(task) in {"parse", "planning"} else "advice"


def _key() -> str:
    return str(config.get_settings().db_path.resolve())


def _lock() -> asyncio.Lock:
    global _guard, _guard_loop
    loop = asyncio.get_running_loop()
    if _guard is None or _guard_loop is not loop:
        _guard, _guard_loop = asyncio.Lock(), loop
    return _guard


def _builtin() -> dict[str, dict[str, Any]]:
    return {
        name: {"id": name, "name": label, "kind": name, "enabled": False,
               "model": "", "base_url": "", "timeout_seconds": 300}
        for name, label in (("codex", "Codex CLI"), ("claude", "Claude Code (facoltativo)"))
    }


async def initialize(*, refresh: bool = False) -> None:
    """Load metadata through the sole application-owned DB connection."""
    global _rows, _routes, _store_key
    if _store_key == _key() and not refresh:
        return
    async with _lock():
        db = await get_db()
        rows = _builtin()
        async with db.execute("SELECT id, payload FROM ai_connections") as cursor:
            for cid, payload in await cursor.fetchall():
                try:
                    row = json.loads(payload)
                    if not isinstance(row, dict) or row.get("id") != cid:
                        raise ValueError("invalid_connection_store")
                    rows[cid] = _validated_connection(row)
                except (ValueError, TypeError, KeyError):
                    raise ValueError("invalid_connection_store") from None
        routes: dict[str, dict[str, Any]] = {}
        async with db.execute("SELECT task, payload FROM ai_routes") as cursor:
            for task, payload in await cursor.fetchall():
                try:
                    if task not in TASKS:
                        raise ValueError("invalid_route_store")
                    routes[task] = _validated_route(json.loads(payload), rows, require_enabled=False)
                except (ValueError, TypeError, KeyError):
                    raise ValueError("invalid_route_store") from None
        _rows, _routes, _store_key = rows, routes, _key()


def has_route(task: str | None = None) -> bool:
    if _store_key != _key():
        return False
    if task is None:
        return any(row.get("primary") for row in _routes.values())
    return bool(_routes.get(task_name(task), {}).get("primary"))


def cache_identity(task: str) -> str:
    name = task_name(task)
    if not has_route(name):
        return "legacy"
    route = _routes[name]
    ids = [route["primary"], *route.get("fallbacks", [])]
    metadata = [{"id": cid, **_rows.get(cid, {}),
                 "credential_version": hashlib.sha256(config.connection_secret(cid).encode()).hexdigest()}
                for cid in ids]
    raw = json.dumps({"task": name, "route": route, "connections": metadata}, sort_keys=True)
    return "route-v1:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _validated_connection(payload: dict[str, Any]) -> dict[str, Any]:
    cid = str(payload.get("id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cid):
        raise ValueError("invalid_connection_id")
    kind = payload.get("kind", "openai_compatible")
    if kind == "hermes":
        kind = "openai_compatible"
    if kind not in {"codex", "claude", "openai_compatible"}:
        raise ValueError("invalid_connection_kind")
    timeout = payload.get("timeout_seconds", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 1800:
        raise ValueError("invalid_connection_timeout")
    name = payload.get("name", cid)
    model = payload.get("model", "")
    enabled = payload.get("enabled", True)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("invalid_connection_name")
    if not isinstance(model, str) or len(model) > 200 or not isinstance(enabled, bool):
        raise ValueError("invalid_connection")
    base = str(payload.get("base_url") or "").strip().rstrip("/")
    if kind == "openai_compatible":
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})):
            raise ValueError("invalid_connection_url")
    elif base:
        raise ValueError("native_runtime_has_no_url")
    return {"id": cid, "kind": kind, "name": name.strip(), "enabled": enabled,
            "model": model.strip(), "base_url": base, "timeout_seconds": int(timeout)}


def _validated_route(choice: Any, rows: dict[str, Any], *, require_enabled: bool = True) -> dict[str, Any]:
    if not isinstance(choice, dict) or set(choice) - {"primary", "fallbacks"}:
        raise ValueError("invalid_route")
    primary, fallbacks = choice.get("primary"), choice.get("fallbacks", [])
    if primary is not None and not isinstance(primary, str):
        raise ValueError("invalid_route")
    if not isinstance(fallbacks, list) or len(fallbacks) > 16 or not all(isinstance(cid, str) for cid in fallbacks):
        raise ValueError("invalid_fallbacks")
    ids = ([primary] if primary else []) + fallbacks
    if (fallbacks and not primary) or len(ids) != len(set(ids)):
        raise ValueError("invalid_fallbacks")
    from app.ai import providers
    for cid in ids:
        if cid.startswith("api:"):
            provider = providers.BY_NAME.get(cid[4:])
            if provider is None or not provider.free:
                raise ValueError("paid_or_unknown_provider")
        elif cid not in rows or (require_enabled and not rows[cid].get("enabled")):
            raise ValueError("connection_unavailable")
    return {"primary": primary or None, "fallbacks": list(fallbacks)}


async def snapshot() -> dict[str, Any]:
    await initialize()
    from app.ai import providers
    records = []
    for row in _rows.values():
        secret = bool(config.connection_secret(row["id"]))
        records.append({**row, "configured": row["enabled"], "has_secret": secret,
                        "capabilities": {"chat": True, "json": True}})
    for provider in providers.PROVIDERS:
        if provider.free:
            records.append({"id": f"api:{provider.name}", "name": f"{provider.name} (API esistente)",
                            "kind": "legacy_api", "enabled": True, "model": "",
                            "configured": providers.is_configured(provider), "has_secret": bool(providers.api_key(provider)),
                            "capabilities": {"chat": True, "json": True}})
    return {"connections": records, "routing": {
        task: _routes.get(task, {"primary": None, "fallbacks": []}) for task in TASKS
    }, "tasks": TASKS}


async def save_connection(payload: dict[str, Any]) -> dict[str, Any]:
    await initialize()
    if not isinstance(payload, dict):
        raise ValueError("invalid_connection")
    allowed = {"id", "name", "kind", "enabled", "model", "base_url", "timeout_seconds", "api_key", "clear_api_key", "delete"}
    if set(payload) - allowed:
        raise ValueError("invalid_connection_fields")
    if any(name in payload and not isinstance(payload[name], bool) for name in ("delete", "clear_api_key")):
        raise ValueError("invalid_connection_flags")
    cid = str(payload.get("id", ""))
    if cid in _builtin() and payload.get("kind", cid) != cid:
        raise ValueError("builtin_connection_kind_fixed")
    async with _lock():
        db = await get_db()
        if payload.get("delete"):
            if cid in _builtin():
                raise ValueError("builtin_connection_cannot_be_deleted")
            if any(cid in [r.get("primary"), *r.get("fallbacks", [])] for r in _routes.values()):
                raise ValueError("connection_in_use")
            await db.execute("DELETE FROM ai_connections WHERE id=?", (cid,))
            await db.commit()
            config.update_connection_secret(cid, None)
            _rows.pop(cid, None)
        else:
            row = _validated_connection({**_rows.get(cid, {}), **payload})
            if "api_key" in payload or payload.get("clear_api_key"):
                secret = "" if payload.get("clear_api_key") else payload["api_key"]
                if not isinstance(secret, str) or len(secret) > 16000:
                    raise ValueError("invalid_connection_secret")
                if row["kind"] != "openai_compatible" and secret:
                    raise ValueError("native_runtime_uses_account")
                config.update_connection_secret(cid, secret or None)
            await db.execute("INSERT INTO ai_connections(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                             (cid, json.dumps(row, ensure_ascii=False)))
            await db.commit()
            _rows[cid] = row
    return await snapshot()


async def save_routes(payload: dict[str, Any]) -> dict[str, Any]:
    await initialize()
    if isinstance(payload, dict) and set(payload) == {"routing"}:
        payload = payload["routing"]
    if not isinstance(payload, dict) or set(payload) - set(TASKS):
        raise ValueError("invalid_routing_task")
    choices = {task: _validated_route(value, _rows) for task, value in payload.items()}
    async with _lock():
        db = await get_db()
        for task, choice in choices.items():
            await db.execute("INSERT INTO ai_routes(task,payload) VALUES (?,?) ON CONFLICT(task) DO UPDATE SET payload=excluded.payload",
                             (task, json.dumps(choice)))
        await db.commit()
        _routes.update(choices)
    return {task: _routes.get(task, {"primary": None, "fallbacks": []}) for task in TASKS}


async def runtime_status(kind: str) -> dict[str, Any]:
    if kind not in {"codex", "claude"}:
        raise ValueError("invalid_runtime")
    return await asyncio.to_thread(runtime.get_runtime_status, kind)


async def connection_status(cid: str) -> dict[str, Any]:
    await initialize()
    row = _rows.get(cid)
    if row is None:
        raise ValueError("unknown_connection")
    if row["kind"] in {"codex", "claude"}:
        return {"connection_id": cid, **await runtime_status(row["kind"])}
    return {"connection_id": cid, "configured": row["enabled"], "kind": row["kind"],
            "has_secret": bool(config.connection_secret(cid))}


async def _native(row: dict[str, Any], messages: list[dict[str, str]], schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
    provider = runtime.create_runtime_provider(row["kind"], row["id"], row.get("model") or None, row["timeout_seconds"])
    _active.add(provider)
    job = asyncio.create_task(asyncio.to_thread(provider.complete, messages, output_schema=schema, max_tokens=max_tokens))
    _active_jobs.add(job)

    def finished(future: asyncio.Task) -> None:
        _active.discard(provider)
        _active_jobs.discard(future)
        # A cancelled HTTP request no longer awaits this worker, but its
        # process cleanup and exception still have an application owner.
        if not future.cancelled():
            future.exception()

    job.add_done_callback(finished)
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        provider.cancel_active()
        with suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(job), timeout=0.25)
        raise


async def _compatible(row: dict[str, Any], messages: list[dict[str, str]], schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
    if not row["model"]:
        raise ValueError("connection_model_required")
    headers = {"Content-Type": "application/json"}
    key = config.connection_secret(row["id"])
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload: dict[str, Any] = {"model": row["model"], "messages": messages, "max_tokens": max_tokens}
    if schema is not None:
        payload["response_format"] = {"type": "json_object"}
        payload["messages"] = [*messages, {"role": "system", "content": "Return JSON following this schema: " + json.dumps(schema)}]
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=row["timeout_seconds"]) as transport:
            response = await transport.post(row["base_url"] + "/chat/completions", headers=headers, json=payload)
            if response.status_code == 429:
                raise runtime.RuntimeConnectionError("quota_exhausted")
            if response.status_code in {401, 403}:
                raise runtime.RuntimeConnectionError("not_authenticated")
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        raise runtime.RuntimeConnectionError("timeout") from None
    except (httpx.HTTPError, ValueError):
        raise runtime.RuntimeConnectionError("protocol_error") from None
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise runtime.RuntimeConnectionError("protocol_error")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise TruncatedCompletionError(row["model"])
    if choice.get("finish_reason") in {"error", "cancelled", "canceled"}:
        reason = choice["finish_reason"]
        raise CallFailed("cancelled" if reason == "canceled" else reason)
    message = choice.get("message") or {}
    if not isinstance(message, dict) or message.get("tool_calls"):
        raise runtime.RuntimeConnectionError("tools_disabled")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise runtime.RuntimeConnectionError("protocol_error")
    if schema is not None:
        try:
            value = json.loads(text)
        except ValueError:
            raise runtime.RuntimeConnectionError("protocol_error") from None
        validate_output(value, schema)
        return value
    return text.strip()


async def complete_route(task: str, system: str, prompt: str, *, json_schema: dict[str, Any] | None = None,
                         max_tokens: int = 4096, history: list[dict[str, str]] | None = None,
                         check: Callable[[str], str] | None = None, cache_key: str | None = None):
    from app.ai import client, model_selector, providers
    from app.orchestrator import cache
    await initialize()
    name = task_name(task)
    if not has_route(name):
        return None
    route = _routes[name]
    messages = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": prompt}]
    if (len(messages) > 200 or any(not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant"}
            or not isinstance(m.get("content"), str) for m in messages)):
        raise ValueError("invalid_ai_messages")
    identity = cache_identity(name)
    scoped_key = cache.make_key("ai:route", cache_key, identity) if cache_key else None
    if scoped_key:
        cached = await cache.get(scoped_key)
        if isinstance(cached, dict) and isinstance(cached.get("text"), str):
            text = cached["text"]
            if not check or not check(text):
                return client.Answer(completion=client.Completion(text=text, model=cached.get("model", ""),
                                     provider=cached.get("provider", ""), finish_reason="stop"))
    last_reason = "failed"
    for cid in [route["primary"], *route.get("fallbacks", [])]:
        observed_provider = None
        model = ""
        try:
            if cid.startswith("api:"):
                provider = providers.BY_NAME.get(cid[4:])
                if provider is None or not provider.free or not providers.is_configured(provider):
                    raise ValueError("connection_unavailable")
                models = await model_selector._models_for(provider, legacy_task(name))
                if provider.name == "openrouter":
                    models = [model for model in models if model.endswith(":free")]
                if not models:
                    raise ValueError("no_models")
                model = models[0]
                observed_provider = provider
                async with client._lock_for(provider.name):
                    text, finish = await client._call(provider, model, system, prompt, max_tokens=max_tokens,
                                                     temperature=0.0 if json_schema else 0.4,
                                                     json_mode=json_schema is not None, task=legacy_task(name), history=history)
                if finish == "length":
                    raise TruncatedCompletionError(model)
                if finish in {"error", "cancelled", "canceled"}:
                    raise CallFailed("cancelled" if finish == "canceled" else finish)
                if not isinstance(text, str) or not text.strip():
                    raise CallFailed("empty")
                value = client._extract_json(text) if json_schema else text
            else:
                row = _rows.get(cid)
                if not row or not row["enabled"]:
                    raise ValueError("connection_unavailable")
                model = row.get("model") or "default"
                value = await (_native(row, messages, json_schema, max_tokens) if row["kind"] in {"codex", "claude"}
                               else _compatible(row, messages, json_schema, max_tokens))
            if json_schema is not None:
                validate_output(value, json_schema)
                if not isinstance(value, dict):
                    raise ValueError("invalid_json_completion")
                text = json.dumps(value, ensure_ascii=False)
            else:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("empty_ai_completion")
                text = value.strip()
                problem = client.GARBLED if client.sembra_ragionamento(text) else (check(text) if check else "")
                if problem:
                    last_reason = problem
                    if observed_provider is not None:
                        await model_selector.record_penalty(model, problem, provider=observed_provider.name)
                    continue
            completion = client.Completion(text=text, provider=cid, model=model, finish_reason="stop")
            if scoped_key:
                await cache.set(scoped_key, completion.__dict__, kind="ai")
            return client.Answer(completion=completion, data=value if isinstance(value, dict) else None)
        except asyncio.CancelledError:
            raise
        except runtime.RuntimeConnectionError as exc:
            last_reason = exc.code
        except CallFailed as exc:
            # Preserve upstream throttle separately from account quota. An
            # explicit API route shares the empirical provider/model ranking
            # with the legacy path, but has no automatic retry or extra route.
            last_reason = client.FAILED if exc.reason == "empty" else exc.reason
            if observed_provider is not None:
                await model_selector.record_penalty(model, exc.reason, provider=observed_provider.name)
        except TruncatedCompletionError:
            last_reason = "truncated"
            if observed_provider is not None:
                await model_selector.record_penalty(model, "truncated", provider=observed_provider.name)
        except Exception:
            last_reason = "failed"
            if observed_provider is not None:
                await model_selector.record_penalty(model, "error", provider=observed_provider.name)
    return client.Answer(reason=last_reason)


async def complete_json(task: str, system: str, prompt: str, *, schema: dict[str, Any], max_tokens: int = 4096,
                        history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Structured completion used by the application's bounded chat loop."""
    from app.ai import client
    answer = await complete_route(task, system, prompt, json_schema=schema, max_tokens=max_tokens, history=history)
    if answer is None:
        answer = await client.complete(task, system, prompt, max_tokens=max_tokens, json_mode=True,
                                       history=history, _skip_route=True)
        if answer.ok:
            value = client._extract_json(answer.text)
            validate_output(value, schema)
            if isinstance(value, dict):
                return value
    elif answer.data is not None:
        return answer.data
    reason = answer.reason if answer else "protocol_error"
    raise runtime.RuntimeConnectionError(reason)


async def probe_connection(cid: str) -> dict[str, Any]:
    await initialize()
    row = _rows.get(cid)
    if not row or not row["enabled"]:
        raise ValueError("unknown_connection")
    messages = [{"role": "user", "content": "Rispondi soltanto con: Pronto."}]
    try:
        value = await (_native(row, messages, None, 32) if row["kind"] in {"codex", "claude"}
                       else _compatible(row, messages, None, 32))
        return {"ok": True, "text": value, "reason": ""}
    except runtime.RuntimeConnectionError as exc:
        return {"ok": False, "text": None, "reason": exc.code}
    except CallFailed as exc:
        return {"ok": False, "text": None, "reason": exc.reason}
    except Exception:
        return {"ok": False, "text": None, "reason": "failed"}


async def shutdown() -> None:
    for provider in tuple(_active):
        provider.cancel_active()
    if _active_jobs:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(job) for job in tuple(_active_jobs)), return_exceptions=True),
                timeout=8,
            )
