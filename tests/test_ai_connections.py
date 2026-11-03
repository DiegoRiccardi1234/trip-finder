"""Task routes use one store, preserve legacy settings, and fail closed."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from app import config
from app.ai import client, connections, runtime
from app.orchestrator import db


@pytest.fixture
async def isolated(tmp_path, monkeypatch):
    await db.close_db()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    config.get_settings.cache_clear()
    await connections.initialize(refresh=True)
    await connections.save_connection({"id": "codex", "enabled": True})
    await connections.save_connection({"id": "claude", "enabled": True})
    yield tmp_path
    await connections.shutdown()
    await db.close_db()
    config.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_codex_route_does_not_call_legacy_even_when_native_fails(isolated, monkeypatch):
    await connections.save_routes({"parse": {"primary": "codex", "fallbacks": []}})

    async def fail(*args):
        raise runtime.RuntimeConnectionError("quota_exhausted")

    async def forbidden(*args, **kwargs):
        pytest.fail("A Codex-only task reached the API pool")

    monkeypatch.setattr(connections, "_native", fail)
    monkeypatch.setattr(client.model_selector, "rank_candidates", forbidden)
    answer = await client.complete_json("parse", "system", "da Torino a Matera", max_tokens=600)
    assert answer.reason == "quota_exhausted"
    assert not answer.ok


@pytest.mark.asyncio
async def test_tasks_and_cache_identity_are_independent(isolated, monkeypatch):
    await connections.save_routes({"parse": {"primary": "codex", "fallbacks": []}})
    assert connections.has_route("parse")
    assert not connections.has_route("planning")
    first = connections.cache_identity("parse")
    await connections.save_connection({"id": "codex", "model": "synthetic-model"})
    assert connections.cache_identity("parse") != first
    assert connections.cache_identity("planning") == "legacy"
    await connections.save_routes({"advice": {"primary": "claude", "fallbacks": []}})
    assert connections._routes["parse"]["primary"] == "codex"
    assert connections._routes["advice"]["primary"] == "claude"


@pytest.mark.asyncio
async def test_fallback_is_only_the_explicit_connection_chain(isolated, monkeypatch):
    await connections.save_routes({"chat": {"primary": "codex", "fallbacks": ["claude"]}})
    calls = []

    async def fake(row, messages, schema, max_tokens):
        calls.append(row["id"])
        if row["id"] == "codex":
            raise runtime.RuntimeConnectionError("not_installed")
        return "Il viaggio è pronto."

    monkeypatch.setattr(connections, "_native", fake)
    result = await client.complete("chat", "system", "test")
    assert result.ok and result.completion.provider == "claude"
    assert calls == ["codex", "claude"]


@pytest.mark.asyncio
async def test_schema_invalid_response_does_not_escape_or_try_an_api(isolated, monkeypatch):
    await connections.save_routes({"parse": {"primary": "codex", "fallbacks": []}})

    async def fake(*args):
        return {"stages": [], "date": "2026-12-01", "data_detta": "yes"}

    monkeypatch.setattr(connections, "_native", fake)
    result = await client.complete_json("parse", "system", "test")
    assert result.reason == "failed"
    assert result.data is None


@pytest.mark.asyncio
async def test_hermes_connection_secret_preserves_legacy_and_stays_out_of_db(isolated):
    config.save_local_secrets({"groq_api_key": "existing-private-key"})
    state = await connections.save_connection({
        "id": "hermes", "kind": "hermes", "name": "Hermes personale",
        "base_url": "http://127.0.0.1:8080/v1", "model": "local-model", "api_key": "new-private-key",
    })
    assert config.connection_secret("hermes") == "new-private-key"
    assert config.read_local_secrets()["groq_api_key"] == "existing-private-key"
    assert "new-private-key" not in json.dumps(state)
    async with (await db.get_db()).execute("SELECT payload FROM ai_connections") as cursor:
        contents = str(await cursor.fetchall())
    assert "private-key" not in contents
    await connections.save_connection({"id": "hermes", "name": "Hermes nuovo nome"})
    assert config.connection_secret("hermes") == "new-private-key"


@pytest.mark.asyncio
async def test_corrupt_route_fails_explicitly_and_does_not_reset_store(isolated):
    connection = await db.get_db()
    await connection.execute("INSERT INTO ai_routes(task,payload) VALUES ('chat','not-json')")
    await connection.commit()
    with pytest.raises(ValueError, match="invalid_route_store"):
        await connections.initialize(refresh=True)
    async with connection.execute("SELECT payload FROM ai_routes WHERE task='chat'") as cursor:
        assert (await cursor.fetchone())[0] == "not-json"


@pytest.mark.asyncio
async def test_disabling_connection_does_not_fall_back_to_legacy(isolated):
    await connections.save_routes({"advice": {"primary": "codex", "fallbacks": []}})
    await connections.save_connection({"id": "codex", "enabled": False})
    await connections.initialize(refresh=True)
    result = await client.complete("advice", "system", "test")
    assert result.reason == "failed"


@pytest.mark.asyncio
async def test_outer_cancellation_stops_only_owned_native_process(isolated, monkeypatch):
    started = threading.Event()
    cancelled = threading.Event()
    instances = []

    class FakeRuntime:
        def __init__(self, cid):
            self.cid = cid
            self.cancelled = False
            instances.append(self)

        def complete(self, *args, **kwargs):
            started.set()
            cancelled.wait(3)
            return "Completato"

        def cancel_active(self):
            self.cancelled = True
            cancelled.set()

    monkeypatch.setattr(runtime, "create_runtime_provider", lambda kind, cid, model, timeout: FakeRuntime(cid))
    task = asyncio.create_task(connections._native(connections._builtin()["codex"], [], None, 50))
    assert await asyncio.to_thread(started.wait, 2)
    unrelated = FakeRuntime("unrelated")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instances[0].cancelled
    assert unrelated.cancelled is False


@pytest.mark.asyncio
async def test_no_native_route_does_not_require_cli_and_preserves_legacy(isolated, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("unassigned route should remain legacy")

    monkeypatch.setattr(connections, "_native", forbidden)
    assert await connections.complete_route("advice", "s", "u") is None


@pytest.mark.asyncio
async def test_route_cannot_add_paid_fallback(isolated):
    with pytest.raises(ValueError, match="paid_or_unknown_provider"):
        await connections.save_routes({"chat": {"primary": "codex", "fallbacks": ["api:openai"]}})


@pytest.mark.asyncio
async def test_explicit_openrouter_api_route_never_selects_a_paid_model(isolated, monkeypatch):
    from app.ai import providers
    monkeypatch.setattr(providers, "is_configured", lambda provider: True)
    await connections.save_routes({"advice": {"primary": "api:openrouter", "fallbacks": []}})

    async def models(*args):
        return ["provider/paid-model", "provider/free-model:free"]

    called = []
    async def call(provider, model, *args, **kwargs):
        called.append(model)
        return "Il viaggio è pronto.", "stop"

    monkeypatch.setattr(client.model_selector, "_models_for", models)
    monkeypatch.setattr(client, "_call", call)
    assert (await client.complete("advice", "system", "test")).ok
    assert called == ["provider/free-model:free"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "reason", "penalty"), [
    ("upstream_429", "rate_limited", "rate_limited"),
    ("length", "truncated", "truncated"),
    ("invalid_json", "failed", "error"),
    ("empty", "failed", "empty"),
    ("check", "unanchored", "unanchored"),
])
async def test_explicit_api_observations_keep_empirical_penalties_without_retries(isolated, monkeypatch, failure, reason, penalty):
    from app.ai import providers
    monkeypatch.setattr(providers, "is_configured", lambda provider: True)
    await connections.save_routes({"advice": {"primary": "api:groq", "fallbacks": []}})
    called, observed = [], []

    async def models(*args):
        return ["fixture-model", "unused-second-model"]

    async def call(provider, model, *args, **kwargs):
        called.append((provider.name, model))
        if failure == "upstream_429":
            raise providers.CallFailed("rate_limited", "private upstream response")
        if failure == "length":
            return '{"ok":true}', "length"
        if failure == "invalid_json":
            return "{broken", "stop"
        if failure == "empty":
            return "", "stop"
        return "Il viaggio è pronto.", "stop"

    async def record(model, reason, provider="openrouter"):
        observed.append((provider, model, reason))

    monkeypatch.setattr(client.model_selector, "_models_for", models)
    monkeypatch.setattr(client.model_selector, "record_penalty", record)
    monkeypatch.setattr(client, "_call", call)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]} if failure == "invalid_json" else None
    result = await connections.complete_route("advice", "system", "test", json_schema=schema,
                                               check=(lambda text: "unanchored") if failure == "check" else None)
    assert result.reason == reason and not result.ok
    assert called == [("groq", "fixture-model")]
    assert observed == [("groq", "fixture-model", penalty)]
    assert "private upstream" not in result.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["error", "cancelled"])
async def test_compatible_declared_failure_cannot_become_successful_text(isolated, monkeypatch, finish):
    await connections.save_connection({"id": "hermes", "kind": "openai_compatible", "name": "Hermes",
                                       "base_url": "http://127.0.0.1:8642/v1", "model": "fixture", "enabled": True})
    await connections.save_routes({"advice": {"primary": "hermes", "fallbacks": []}})
    requests = []

    class Transport:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            requests.append(url)
            import httpx
            return httpx.Response(200, json={"choices": [{"finish_reason": finish,
                "message": {"content": "Questo testo apparentemente valido non deve essere accettato."}}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(connections.httpx, "AsyncClient", Transport)
    result = await connections.complete_route("advice", "system", "test")
    assert not result.ok and result.reason == finish
    assert len(requests) == 1


@pytest.mark.parametrize("invalid_date", ["not-a-date", "2026-02-30", "2026-13-01"])
def test_native_trip_schema_rejects_dates_instead_of_inventing_them(invalid_date):
    from app.ai.runtime_types import validate_output
    with pytest.raises(ValueError, match="schema"):
        validate_output({"stages": [], "date": invalid_date, "data_detta": True}, connections.JSON_SCHEMAS["parse"])


@pytest.mark.asyncio
async def test_tool_search_skips_all_ai_hubs(monkeypatch, query, torino, matera):
    from app.orchestrator import search_service

    class Resolver:
        def resolve(self, value, **kwargs):
            return torino if value == "Torino" else matera

    async def forbidden(*args):
        pytest.fail("MCP/tool search invoked AI")

    monkeypatch.setattr(search_service, "get_resolver", lambda: Resolver())
    monkeypatch.setattr(search_service.SearchService, "_suggest_hubs", forbidden)
    monkeypatch.setattr(search_service.composer, "plan", lambda *args: [])
    monkeypatch.setattr(search_service.composer, "required_hops", lambda *args: {})
    monkeypatch.setattr(search_service.composer, "tasks_for_tier", lambda *args, **kwargs: [])
    service = search_service.SearchService()
    events = [event async for event in service.run(query, allow_ai_hubs=False)]
    assert events[-1].event == "done"
