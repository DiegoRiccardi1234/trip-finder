"""Local assistant boundary: one store, no nested AI, proposals and MCP credentials."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import agent_security as security, config
from app.agent_router import build_router
from app.agent_service import AgentService, AgentToolError
from app.models import Itinerary, Mode, SearchQuery
from app.orchestrator import db, profile
from tests.conftest import TORINO_PN, BARI_C, at, make_leg


@pytest.fixture
async def service(tmp_path, monkeypatch):
    await db.close_db()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(db_path=tmp_path / "state.db"))
    security._sessions.clear()
    instance = AgentService()
    await instance.initialize()
    yield instance
    await instance.close()
    await db.close_db()
    security._sessions.clear()


def plan(**values):
    return {"stages": [{"origin": "Torino", "destination": "Bari", "date": (date.today()+timedelta(days=7)).isoformat()}], **values}


def itinerary():
    return Itinerary(id="upstream-id", legs=[make_leg("test", Mode.RAIL, TORINO_PN, BARI_C, at(9), at(14), 35)])


async def fake_search(self, query, *, allow_ai_hubs=True):
    from app.orchestrator.search_service import SearchEvent
    assert allow_ai_hubs is False
    yield SearchEvent("itineraries", {"items": [itinerary()], "partial": False})
    yield SearchEvent("done", {"itineraries": 1})


async def completed_search(service, monkeypatch, request_id="search-1"):
    from app.orchestrator.search_service import SearchService
    monkeypatch.setattr(SearchService, "run", fake_search)
    operation = await service.start_search(plan(), request_id)
    task = service._tasks.get(operation["operation_id"])
    if task:
        await task
    return await service.get_operation(operation["operation_id"])


async def test_search_does_not_infer_and_replay_preserves_operation_and_snapshot(service, monkeypatch):
    result = await completed_search(service, monkeypatch)
    assert result["status"] == "completed"
    found = result["result"]["stages"][0]["itineraries"][0]
    assert found["id"] != "upstream-id"
    replay = await service.start_search(plan(), "search-1")
    assert replay == result
    with pytest.raises(AgentToolError, match="request_id_conflict"):
        await service.start_search(plan(pax=2), "search-1")


@pytest.mark.parametrize("change", [{"pax": True}, {"pax": 10}, {"max_changes": 4}, {"modes": []}, {"max_budget": -1}, {"unknown": 1}, {"stages": []}, {"stages": [{"origin": "Torino", "destination": "Bari", "date": "2000-01-01"}]}])
async def test_invalid_search_is_rejected_before_any_operation(service, change):
    with pytest.raises(AgentToolError, match="invalid_trip_plan"):
        await service.start_search(plan(**change), "bad-plan")
    assert await service.list_operations() == []


async def test_save_is_ui_proposal_with_stable_snapshot_and_replay(service, monkeypatch):
    operation = await completed_search(service, monkeypatch)
    item = operation["result"]["stages"][0]["itineraries"][0]
    proposal = await service.propose_save_itinerary(operation["operation_id"], item["id"], "Vacanza", "save-1")
    assert proposal["status"] == "pending"
    assert await service.list_saved() == []
    confirmed = await service.decide_proposal(proposal["proposal_id"], True)
    assert confirmed["status"] == "confirmed"
    assert await service.decide_proposal(proposal["proposal_id"], True) == confirmed
    replay = await service.propose_save_itinerary(operation["operation_id"], item["id"], "Vacanza", "save-1")
    assert replay == confirmed
    saved = await service.get_saved(confirmed["result"]["saved_itinerary_id"])
    assert saved["snapshot"]["id"] == item["id"]
    assert saved["historical"] is True
    assert len(await service.list_saved()) == 1


async def test_profile_proposals_detect_stale_data_and_cannot_certify_discount(service):
    proposal = await service.propose_profile_change("preferences", {"home": "Torino"}, "prefs")
    await profile.set_preferences({"home": "Roma"})
    with pytest.raises(AgentToolError, match="profile_changed_propose_again"):
        await service.decide_proposal(proposal["proposal_id"], True)
    assert (await profile.get_preferences())["home"] == "Roma"
    card = await service.propose_profile_change("discount", {"name": "Carta", "value": 20, "verified": True}, "card")
    assert card["payload"]["verified"] is False
    confirmed = await service.decide_proposal(card["proposal_id"], True)
    assert confirmed["result"]["discount"]["verified"] is False
    await service.decide_proposal(card["proposal_id"], True)
    assert len(await profile.list_discounts()) == 1


async def test_confirmation_rolls_back_with_concurrent_cache_commit(service, monkeypatch):
    """An external COMMIT cannot publish half of a failed confirmation."""
    proposal = await service.propose_profile_change("preferences", {"home": "Torino"}, "atomic")
    connection = await db.get_db()
    await connection.execute("CREATE TRIGGER fail_confirmation BEFORE UPDATE OF status ON agent_proposals WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    await connection.commit()
    results = await asyncio.gather(service.decide_proposal(proposal["proposal_id"], True), connection.commit(), return_exceptions=True)
    assert isinstance(results[0], Exception)
    assert await profile.get_preferences() == {}
    assert (await service.get_operation(proposal["proposal_id"]))["status"] == "pending"
    await connection.execute("DROP TRIGGER fail_confirmation")
    await connection.commit()
    assert (await service.decide_proposal(proposal["proposal_id"], True))["status"] == "confirmed"


async def test_atomic_callback_queue_blocks_unrelated_writer_until_complete(service):
    connection = await db.get_db()
    entered, release = threading.Event(), threading.Event()

    def apply(raw):
        raw.execute("INSERT INTO preferences VALUES('home','\"Torino\"')")
        entered.set()
        assert release.wait(3)
        raw.execute("INSERT INTO preferences VALUES('pax','2')")
    task = asyncio.create_task(db.atomic_write(apply))
    assert await asyncio.to_thread(entered.wait, 3)
    other = asyncio.create_task(connection.commit())
    await asyncio.sleep(.02)
    assert not other.done()
    release.set()
    await asyncio.gather(task, other)
    assert await profile.get_preferences() == {"home": "Torino", "pax": 2}


async def test_cancellation_keeps_partial_results_and_restart_marks_interrupted(service, monkeypatch):
    from app.orchestrator.search_service import SearchEvent, SearchService
    partial = asyncio.Event()
    async def slow(self, query, *, allow_ai_hubs):
        yield SearchEvent("itineraries", {"items": [itinerary()], "partial": True})
        partial.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(SearchService, "run", slow)
    operation = await service.start_search(plan(), "slow")
    await partial.wait()
    cancelled = await service.cancel_operation(operation["operation_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["result"]["stages"][0]["itineraries"]
    connection = await db.get_db()
    await connection.execute("UPDATE agent_operations SET status='running' WHERE id=?", (operation["operation_id"],))
    await connection.commit()
    await service.initialize()
    interrupted = await service.get_operation(operation["operation_id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["result"]["stages"][0]["itineraries"]


async def test_context_import_stored_server_side_and_empty_legs_rejected(service):
    context = {"query": SearchQuery(origin="Torino", destination="Bari", date=date.today()).model_dump(mode="json"), "itineraries": [itinerary().model_dump(mode="json")]}
    conversation = await service.create_conversation(context=context)
    operation = await service.get_operation(conversation["context"]["operation_id"])
    assert operation["kind"] == "imported"
    assert operation["result"]["source"] == "imported"
    context["itineraries"][0]["legs"] = []
    with pytest.raises(AgentToolError, match="invalid_search_context"):
        await service.create_conversation(context=context)


async def test_roundtrip_import_keeps_both_directions_and_refuses_half_return(service):
    query = SearchQuery(origin="Torino", destination="Bari", date=date.today()).model_dump(mode="json")
    returning = {**query, "origin": "Bari", "destination": "Torino"}
    context = {"query": query, "itineraries": [itinerary().model_dump(mode="json")],
               "return_query": returning, "return_itineraries": [itinerary().model_dump(mode="json")]}
    conversation = await service.create_conversation(context=context)
    result = (await service.get_operation(conversation["context"]["operation_id"]))["result"]
    assert [stage["label"] for stage in result["stages"]] == ["Andata", "Ritorno"]
    assert result["stages"][0]["itineraries"][0]["id"] != result["stages"][1]["itineraries"][0]["id"]
    context.pop("return_itineraries")
    with pytest.raises(AgentToolError, match="invalid_search_context"):
        await service.create_conversation(context=context)


@pytest.fixture
async def api(service):
    app = FastAPI()
    app.include_router(build_router(service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8099") as client:
        yield client


async def ui_session(api):
    response = await api.post("/api/agent/ui/session", headers={"Origin": "http://127.0.0.1:8099", "x-tripfinder-ui": "1"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    return {"Origin": "http://127.0.0.1:8099", "x-tripfinder-csrf": response.json()["csrf_token"]}


async def test_ui_and_mcp_credentials_are_not_interchangeable(api, service):
    assert (await api.get("/api/agent/proposals")).status_code == 403
    assert (await api.post("/api/agent/ui/session", headers={"Origin": "https://evil.test", "x-tripfinder-ui": "1"})).status_code == 403
    headers = await ui_session(api)
    assert (await api.post("/api/agent/tools/tripfinder_get_context", json={})).status_code == 401
    assert (await api.post("/api/agent/proposals/profile", json={"kind": "preferences", "payload": {"home": "Torino"}, "request_id": "ui-1"})).status_code == 403
    proposal = await api.post("/api/agent/proposals/profile", headers=headers, json={"kind": "preferences", "payload": {"home": "Torino"}, "request_id": "ui-1"})
    assert proposal.status_code == 200
    token = security.capability(create=True)
    api.cookies.clear()
    bearer = {"Authorization": "Bearer " + token}
    assert (await api.post("/api/agent/tools/tripfinder_get_context", headers=bearer, json={})).status_code == 200
    assert (await api.post(f"/api/agent/proposals/{proposal.json()['proposal_id']}/confirm", headers={**bearer, "Origin": "http://127.0.0.1:8099"})).status_code == 403
    assert (await api.post("/api/agent/tools/tripfinder_get_context", headers={**bearer, "Origin": "https://evil.test"}, json={})).status_code == 403


async def test_setup_contains_paths_not_capability_and_descriptor_identity(api):
    headers = await ui_session(api)
    response = await api.post("/api/agent/setup", headers=headers)
    assert response.status_code == 200
    token = security.capability()
    assert token not in response.text
    assert "mcp-token.txt" in response.text
    assert set(response.json()["configs"]) >= {"codex", "hermes", "claude_desktop"}
    descriptor = json.loads((config.DATA_DIR / "runtime.json").read_text())
    assert descriptor["base_url"] == "http://127.0.0.1:8099"
    wrong = await api.post("/api/agent/tools/tripfinder_get_context", headers={"Authorization": "Bearer " + token, "x-tripfinder-instance": "wrong"}, json={})
    assert wrong.status_code == 409


@pytest.mark.parametrize("organized", [False, True])
async def test_frozen_setup_selects_console_adapter_for_each_layout(service, tmp_path, monkeypatch, organized):
    from app.install_layout import LAYOUT_MARKER
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(security.sys, "frozen", True, raising=False)
    if organized:
        (root / "app").mkdir()
        (root / "app" / LAYOUT_MARKER).write_text(json.dumps({"version": 1}), encoding="utf-8")
    monkeypatch.setattr(security.sys, "executable", str(root / "app" / "TripFinder.exe" if organized else root / "TripFinder.exe"))
    setup = security.setup("http://127.0.0.1:8099")
    entry = setup["configs"]["claude_desktop"]["mcpServers"]["tripfinder"]
    assert entry["command"] == str(root / ("TripFinder.exe" if organized else "TripFinderMCP.exe"))
    assert entry["args"][0] == ("--mcp-stdio" if organized else "--stdio")
    assert security.capability() not in json.dumps(setup)


async def test_frozen_setup_uses_physical_runtime_with_separate_workspace(service, tmp_path, monkeypatch):
    from app.install_layout import LAYOUT_MARKER
    physical = tmp_path / "program"
    (physical / "app").mkdir(parents=True)
    (physical / "app" / LAYOUT_MARKER).write_text(json.dumps({"version": 1}), encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", tmp_path / "different-workspace")
    monkeypatch.setattr(security.sys, "frozen", True, raising=False)
    monkeypatch.setattr(security.sys, "executable", str(physical / "app" / "TripFinder.exe"))
    setup = security.setup("http://127.0.0.1:8099")
    entry = setup["configs"]["claude_desktop"]["mcpServers"]["tripfinder"]
    assert entry["command"] == str(physical / "TripFinder.exe")
    assert str(config.DATA_DIR / "runtime.json") in entry["args"]
    assert str(security.token_path()) in entry["args"]


async def test_source_setup_uses_absolute_script_and_preserves_stdio_flag(service):
    setup = security.setup("http://127.0.0.1:8099")
    entry = setup["configs"]["claude_desktop"]["mcpServers"]["tripfinder"]
    assert entry["command"] == security.sys.executable
    assert entry["args"][0].endswith("mcp_server.py")
    assert "--stdio" in entry["args"]


@pytest.mark.parametrize("flag", ["--stdio", "--mcp-stdio"])
async def test_source_stdio_protocol_from_another_directory_without_server_or_inference(service, tmp_path, flag):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    security.capability(create=True)
    script = Path(__file__).resolve().parents[1] / "app" / "mcp_server.py"
    params = StdioServerParameters(command=security.sys.executable,
        args=[str(script), flag, "--token-file", str(security.token_path()), "--url", "http://127.0.0.1:8099"], cwd=str(tmp_path))
    async with Client(params, read_timeout_seconds=15) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 9
        assert "tripfinder_get_context" in {tool.name for tool in tools.tools}


async def test_chat_turn_uses_structured_task_and_replays_without_extra_model(service, monkeypatch):
    from app.ai import connections
    calls = []
    async def completion(task, system, prompt, **kwargs):
        calls.append(task)
        return {"action": "reply", "text": "Per quando vuoi partire?", "arguments_json": "{}"}
    monkeypatch.setattr(connections, "complete_json", completion)
    conversation = await service.create_conversation()
    result = await service.chat(conversation["id"], "Andiamo a Matera", "chat-1")
    replay = await service.chat(conversation["id"], "Andiamo a Matera", "chat-1")
    assert result == replay
    assert calls == ["chat"]
    assert result["conversation"]["messages"][-1]["content"] == "Per quando vuoi partire?"


async def test_chat_can_lower_but_not_raise_model_call_budget(service, monkeypatch):
    from app.ai import connections
    calls = []
    async def completion(*args, **kwargs):
        calls.append(1)
        assert json.loads(args[2])["remaining_calls"] == 1
        return {"action": "context", "text": "", "arguments_json": "{}"}
    monkeypatch.setattr(connections, "complete_json", completion)
    conversation = await service.create_conversation()
    with pytest.raises(AgentToolError, match="chat_call_limit"):
        await service.chat(conversation["id"], "Ciao", "one-call", max_calls=1)
    assert len(calls) == 1
    with pytest.raises(AgentToolError, match="invalid_chat_call_limit"):
        await service.chat(conversation["id"], "Ciao", "invalid-budget", max_calls=7)


async def test_chat_search_same_service_then_proposal_waits_for_human(service, monkeypatch):
    from app.ai import connections
    from app.orchestrator.search_service import SearchService
    monkeypatch.setattr(SearchService, "run", fake_search)
    calls = []
    async def completion(task, system, prompt, **kwargs):
        calls.append(task)
        data = json.loads(prompt)
        if not data["observations"]:
            return {"action": "search", "text": "", "arguments_json": json.dumps({"plan": plan()})}
        output = data["observations"][0]["result"]
        if len(data["observations"]) == 1:
            return {"action": "propose_save", "text": "", "arguments_json": json.dumps({"operation_id": output["operation_id"], "itinerary_id": output["result"]["stages"][0]["itineraries"][0]["id"], "name": "Viaggio"})}
        return {"action": "reply", "text": "Ho preparato il salvataggio: confermalo nell'app.", "arguments_json": "{}"}
    monkeypatch.setattr(connections, "complete_json", completion)
    conversation = await service.create_conversation()
    result = await service.chat(conversation["id"], "Cerca un viaggio Torino Bari e proponi di salvarlo", "travel-chat")
    assert calls == ["chat"] * 3
    assert result["operations"][0]["status"] == "completed"
    assert result["proposals"][0]["status"] == "pending"
    assert await service.list_saved() == []


async def test_chat_cancel_is_server_side_and_six_call_limit(service, monkeypatch):
    from app.ai import connections
    entered = asyncio.Event()
    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(connections, "complete_json", waiting)
    conversation = await service.create_conversation()
    task = asyncio.create_task(service.chat(conversation["id"], "Ciao", "cancel-chat"))
    await entered.wait()
    assert (await service.cancel_chat(conversation["id"]))["cancelled"]
    assert (await asyncio.gather(task, return_exceptions=True))[0].__class__ is asyncio.CancelledError
    calls = []
    async def loop(*args, **kwargs):
        calls.append(1)
        return {"action": "context", "text": "", "arguments_json": "{}"}
    monkeypatch.setattr(connections, "complete_json", loop)
    with pytest.raises(AgentToolError, match="chat_call_limit"):
        await service.chat(conversation["id"], "Ciao", "limit-chat")
    assert len(calls) == 6


async def test_mcp_official_registry_has_nine_tools_and_no_confirmation(service):
    from app.mcp_server import create_mcp_server
    server = create_mcp_server(service.dispatch)
    tools = await server.list_tools()
    names = [tool.name for tool in tools]
    assert len(names) == 9
    assert "tripfinder_start_search" in names
    assert all("confirm" not in name for name in names)


async def test_official_sdk_negotiation_calls_async_dispatch_and_preserves_errors(service):
    from mcp import Client
    from app.mcp_server import create_mcp_http_app, create_mcp_server, ToolError
    calls = []
    async def dispatch(name, arguments):
        calls.append((name, arguments))
        if arguments.get("operation_id"):
            raise ToolError("operation_not_found")
        return {"preferences": {}}
    async with Client(create_mcp_server(dispatch)) as client:
        assert len((await client.list_tools()).tools) == 9
        result = await client.call_tool("tripfinder_get_context", {})
        assert result.structured_content == {"preferences": {}}
        error = await client.call_tool("tripfinder_get_operation", {"operation_id": "missing"})
        assert error.is_error
        assert "operation_not_found" in str(error.content)
    assert calls[0][0] == "tripfinder_get_context"


async def test_real_mcp_http_transport_requires_capability_and_rejects_foreign_origin(service):
    from app.mcp_server import create_mcp_http_app
    server, mcp_app = create_mcp_http_app(service)
    app = FastAPI()
    app.mount("/mcp", mcp_app)
    async with server.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8099") as client:
            payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
            headers = {"Accept": "application/json, text/event-stream"}
            assert (await client.post("/mcp/", json=payload, headers=headers)).status_code == 401
            headers["Authorization"] = "Bearer " + security.capability(create=True)
            response = await client.post("/mcp/", json=payload, headers=headers)
            assert response.status_code == 200
            assert response.json()["result"]["serverInfo"]["name"] == "Trip Finder"
            headers["MCP-Protocol-Version"] = response.json()["result"]["protocolVersion"]
            result = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
            assert len(result.json()["result"]["tools"]) == 9
            assert (await client.post("/mcp/", json=payload, headers={**headers, "Origin": "https://evil.test"})).status_code == 403


@pytest.mark.parametrize("code,status", [("quota_exhausted", 429), ("not_authenticated", 401), ("timeout", 503), ("tools_disabled", 503)])
async def test_chat_keeps_safe_native_failure_reason(service, monkeypatch, code, status):
    from app.ai import connections, runtime
    async def fail(*args, **kwargs):
        raise runtime.RuntimeConnectionError(code)
    monkeypatch.setattr(connections, "complete_json", fail)
    conversation = await service.create_conversation()
    with pytest.raises(AgentToolError) as caught:
        await service.chat(conversation["id"], "Ciao", "failure")
    assert caught.value.code == code
    assert caught.value.status_code == status
    assert (await service.get_conversation(conversation["id"]))["messages"][-1]["error"] == code


async def test_stdio_dispatch_uses_running_app_descriptor_and_masks_transport_errors(service, tmp_path):
    from app.mcp_server import RemoteDispatcher, ToolError
    security.register_runtime("http://127.0.0.1:8099")
    security.capability(create=True)
    descriptor = config.DATA_DIR / "runtime.json"
    remote = RemoteDispatcher(security.token_path(), descriptor, instance_id=security.instance_id())
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"preferences": {}})
    await remote.client.aclose()
    remote.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await remote("tripfinder_get_context", {}) == {"preferences": {}}
        assert str(calls[0].url).startswith("http://127.0.0.1:8099/")
        assert calls[0].headers["x-tripfinder-instance"] == security.instance_id()
        security.register_runtime("http://127.0.0.1:8102")
        await remote("tripfinder_get_context", {})
        assert ":8102/" in str(calls[-1].url)
        descriptor.unlink()
        with pytest.raises(ToolError, match="tripfinder_unreachable_open_the_local_app"):
            await remote("tripfinder_get_context", {})
    finally:
        await remote.close()
