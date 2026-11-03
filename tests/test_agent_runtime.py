"""Real subprocesses speaking small, deterministic native runtime protocols."""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest

from app.ai import runtime
from app.ai.runtime_types import TruncatedCompletionError

FAKE_RUNTIME = r"""
import json, sys, time
mode = sys.argv[sys.argv.index('--fixture-mode') + 1]
def emit(value):
    print(json.dumps(value), flush=True)
if '--version' in sys.argv:
    print('fixture-runtime 1.2.3')
elif 'auth' in sys.argv:
    emit({'loggedIn': mode != 'unauth', 'authMethod': 'api_key' if mode == 'api' else 'claude.ai', 'subscriptionType': 'max', 'email': 'private@example.org', 'accessToken': 'secret-fixture'})
    sys.exit(1 if mode == 'unauth' else 0)
elif '-p' in sys.argv:
    sys.stdin.read()
    if mode == 'hang':
        time.sleep(60)
    elif mode == 'quota':
        emit({'is_error': True, 'subtype': 'error_during_execution', 'errors': ['quota exhausted secret-fixture']})
    elif mode == 'truncated':
        emit({'is_error': True, 'subtype': 'error_max_structured_output_retries'})
    else:
        result = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Risposta sintetica', 'usage': {'input_tokens': 11, 'output_tokens': 7}}
        if mode == 'cached':
            result['usage'].update(cache_read_input_tokens=100, cache_creation_input_tokens=20)
        if '--json-schema' in sys.argv:
            schema = json.loads(sys.argv[sys.argv.index('--json-schema') + 1])
            result['structured_output'] = {'json': json.dumps({'valutazioni': [{'id': 12, 'score': 8}], 'custom': {'nested': True}})} if 'json' in schema.get('properties', {}) else {'ok': True}
        emit(result)
else:
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get('method')
        if method == 'initialize':
            emit({'id': message['id'], 'result': {'userAgent': 'fixture'}})
        elif method == 'account/read':
            account = None if mode == 'unauth' else {'type': 'apiKey' if mode == 'api' else 'chatgpt', 'planType': 'pro', 'email': 'private@example.org'}
            emit({'id': message['id'], 'result': {'account': account, 'requiresOpenaiAuth': True}})
        elif method == 'account/rateLimits/read':
            emit({'id': message['id'], 'result': {'rateLimits': {'primary': {'usedPercent': 12, 'windowDurationMins': 300, 'resetsAt': 1900000000, 'credential': 'secret-fixture'}}}})
        elif method == 'model/list':
            emit({'id': message['id'], 'result': {'data': [{'model': 'fixture-model', 'isDefault': True}], 'nextCursor': None}})
        elif method == 'thread/start':
            emit({'method': 'thread/started', 'params': {'thread': {'id': 'thread-fixture'}}})
            emit({'id': message['id'], 'result': {'thread': {'id': 'thread-fixture'}}})
        elif method == 'turn/start':
            emit({'id': message['id'], 'result': {'turn': {'id': 'turn-fixture', 'status': 'inProgress'}}})
            if mode == 'slow':
                time.sleep(.2)
            if mode == 'hang':
                time.sleep(60)
            elif mode == 'tool':
                emit({'id': 999, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread-fixture'}})
            elif mode == 'quota':
                emit({'method': 'turn/completed', 'params': {'threadId': 'thread-fixture', 'turn': {'status': 'failed', 'error': {'codexErrorInfo': 'usageLimitExceeded', 'message': 'secret-fixture'}}}})
            elif mode == 'truncated':
                emit({'method': 'turn/completed', 'params': {'threadId': 'thread-fixture', 'turn': {'status': 'failed', 'error': {'codexErrorInfo': 'contextWindowExceeded'}}}})
            else:
                schema = message['params'].get('outputSchema')
                text = 'Risposta sintetica'
                if schema:
                    value = {'json': json.dumps({'valutazioni': [{'id': 12, 'score': 8}], 'custom': {'nested': True}})} if 'json' in schema.get('properties', {}) else {'ok': True}
                    text = json.dumps(value)
                emit({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread-fixture', 'turnId': 'turn-fixture', 'tokenUsage': {'last': {'inputTokens': 11, 'outputTokens': 7}}}})
                emit({'method': 'item/completed', 'params': {'threadId': 'thread-fixture', 'turnId': 'turn-fixture', 'item': {'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer', 'text': text}}})
                emit({'method': 'turn/completed', 'params': {'threadId': 'thread-fixture', 'turn': {'id': 'turn-fixture', 'status': 'completed', 'items': []}}})
"""


@pytest.fixture
def native(monkeypatch, tmp_path):
    script = tmp_path / "native_fixture.py"
    script.write_text(FAKE_RUNTIME, encoding="utf-8")
    state = {"mode": "success", "calls": [], "processes": [], "live_counts": []}
    spawn = runtime._spawn

    def fake_spawn(args, cwd):
        state["calls"].append(args)
        # Windows venv python.exe can be a launcher process. The fixture must
        # own the interpreter that actually runs it, just like the native
        # codex.exe selected in production, so cancellation has no orphan.
        interpreter = getattr(sys, "_base_executable", sys.executable)
        process = spawn(
            [interpreter, str(script), "--fixture-mode", state["mode"], *args[1:]], cwd
        )
        state["processes"].append(process)
        state["live_counts"].append(sum(p.poll() is None for p in state["processes"]))
        return process

    monkeypatch.setattr(runtime, "_find_cli", lambda kind: sys.executable)
    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(
        runtime, "_codex_config", lambda: {"features.shell_tool": False, "web_search": "disabled"}
    )
    return state


def test_child_environment_never_inherits_billing_or_wrapper_identity(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CODEX_THREAD_ID",
        "CODEX_REMOTE_URL",
        "NODE_OPTIONS",
        "CLAUDE_CODE_USE_BEDROCK",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "MCP_TOKEN",
        "HERMES_PASSWORD",
        "REGISTRY_SECRET",
        "SERVICE_KEY",
        "EBAY_APP_ID",
        "EBAY_CERT_ID",
    ):
        monkeypatch.setenv(key, "secret-fixture")
    monkeypatch.setenv("CODEX_HOME", "native-auth-location")
    child = runtime._child_env()
    assert "secret-fixture" not in child.values()
    assert child["CODEX_HOME"] == "native-auth-location"


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_native_status_uses_auth_probe_without_exposing_identity_or_credentials(native, kind):
    result = runtime.get_runtime_status(kind)
    assert result["authenticated"] is True
    assert result["version"] == "1.2.3"
    assert "secret-fixture" not in json.dumps(result)
    assert "private@example.org" not in json.dumps(result)
    assert not any("-p" in args for args in native["calls"])
    if kind == "codex":
        assert result["quota"]["primary"]["usedPercent"] == 12
    else:
        assert result["quota"] is None


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize(
    ("mode", "expected"), [("api", "subscription_required"), ("unauth", "not_authenticated")]
)
def test_api_auth_and_missing_login_fail_before_inference(native, kind, mode, expected):
    native["mode"] = mode
    assert runtime.get_runtime_status(kind)["error"] == expected
    provider = runtime.create_runtime_provider(kind, f"{kind}-auth-{mode}")
    with pytest.raises(runtime.RuntimeConnectionError) as error:
        provider.complete_text("secret-fixture prompt")
    assert error.value.code == expected
    assert "secret-fixture" not in str(error.value)
    assert not any("-p" in args for args in native["calls"])


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_native_json_preserves_arbitrary_scoring_shape_and_token_usage(native, kind):
    provider = runtime.create_runtime_provider(kind, f"{kind}-json")
    answer = provider.complete_json("Return the requested scoring object")
    assert answer == {"valutazioni": [{"id": 12, "score": 8}], "custom": {"nested": True}}
    assert provider.last_usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert all(p.poll() is not None for p in native["processes"])


def test_claude_print_mode_disables_tools_and_settings_without_bare_or_prompt_arguments(native):
    provider = runtime.create_runtime_provider("claude", "claude-options", model="sonnet")
    assert provider.complete_text("private prompt never in argv") == "Risposta sintetica"
    args = next(args for args in native["calls"] if "-p" in args)
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in args and "--safe-mode" in args
    assert "--no-session-persistence" in args and "--bare" not in args
    assert "private prompt never in argv" not in " ".join(args)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_explicit_json_schema_is_not_rewrapped(native, kind):
    provider = runtime.NativeRuntimeProvider(kind, f"{kind}-schema", None, 5)
    answer = provider.complete_json(
        "Return ok true",
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    assert answer == {"ok": True}


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_quota_failure_never_propagates_native_error_payload(native, kind):
    native["mode"] = "quota"
    provider = runtime.create_runtime_provider(kind, f"{kind}-quota")
    with pytest.raises(runtime.RuntimeConnectionError) as error:
        provider.complete_text("prompt")
    assert error.value.code == "quota_exhausted"
    assert error.value.status_code == 429
    assert "secret-fixture" not in str(error.value)


def test_claude_structured_output_retry_exhaustion_is_truncation(native):
    native["mode"] = "truncated"
    provider = runtime.create_runtime_provider("claude", "claude-truncated")
    with pytest.raises(TruncatedCompletionError):
        provider.complete_json("prompt")


def test_claude_usage_includes_cached_input_without_inventing_money(native):
    native["mode"] = "cached"
    provider = runtime.create_runtime_provider("claude", "claude-cache-usage")
    provider.complete_text("prompt")
    assert provider.last_usage == {
        "prompt_tokens": 131,
        "completion_tokens": 7,
        "total_tokens": 138,
    }


def test_codex_context_limit_does_not_return_partial_json(native):
    native["mode"] = "truncated"
    provider = runtime.create_runtime_provider("codex", "codex-truncated")
    with pytest.raises(TruncatedCompletionError):
        provider.complete_json("prompt")


def test_codex_rejects_server_initiated_tool_approval(native):
    native["mode"] = "tool"
    provider = runtime.create_runtime_provider("codex", "codex-tools")
    with pytest.raises(runtime.RuntimeConnectionError) as error:
        provider.complete_text("prompt")
    assert error.value.code == "tools_disabled"


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_timeout_closes_only_owned_native_process(native, kind):
    native["mode"] = "hang"
    provider = runtime.create_runtime_provider(kind, f"{kind}-timeout", timeout_seconds=1)
    with pytest.raises(runtime.RuntimeConnectionError) as error:
        provider.complete_text("prompt")
    assert error.value.code == "timeout"
    assert all(p.poll() is not None for p in native["processes"])


def test_cancel_terminates_active_request_and_blocks_pending_work_until_reset(native):
    native["mode"] = "hang"
    provider = runtime.NativeRuntimeProvider("codex", "codex-cancel", None, 10)
    errors = []

    def work():
        try:
            provider.complete_text("prompt")
        except runtime.RuntimeConnectionError as exc:
            errors.append(exc.code)

    worker = threading.Thread(target=work)
    worker.start()
    deadline = time.monotonic() + 3
    while provider._active_process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    provider.cancel_active()
    worker.join(timeout=4)
    assert not worker.is_alive()
    assert errors == ["cancelled"]
    calls = len(native["calls"])
    with pytest.raises(runtime.RuntimeConnectionError, match="annullata"):
        provider.complete_text("pending offer")
    assert len(native["calls"]) == calls
    provider.reset_cancel()
    native["mode"] = "success"
    assert provider.complete_text("new operation") == "Risposta sintetica"


def test_one_active_request_per_connection_even_with_two_provider_instances(native):
    native["mode"] = "slow"
    first = runtime.NativeRuntimeProvider("codex", "same-connection", None, 5)
    second = runtime.NativeRuntimeProvider("codex", "same-connection", None, 5)
    results = []
    threads = [
        threading.Thread(target=lambda p=p: results.append(p.complete_text("prompt")))
        for p in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert results == ["Risposta sintetica", "Risposta sintetica"]
    assert max(native["live_counts"]) == 1


def test_cancellation_releases_waiting_request_without_starting_another_process(native):
    native["mode"] = "hang"
    provider = runtime.NativeRuntimeProvider("codex", "codex-queued-cancel", None, 10)
    errors = []

    def work():
        try:
            provider.complete_text("prompt")
        except runtime.RuntimeConnectionError as exc:
            errors.append(exc.code)

    first = threading.Thread(target=work)
    first.start()
    deadline = time.monotonic() + 3
    while not native["processes"] and time.monotonic() < deadline:
        time.sleep(0.01)
    waiting = threading.Thread(target=work)
    waiting.start()
    time.sleep(0.05)
    provider.cancel_active()
    first.join(timeout=4)
    waiting.join(timeout=4)
    assert not first.is_alive() and not waiting.is_alive()
    assert errors == ["cancelled", "cancelled"]
    assert len(native["processes"]) == 1


def test_queue_wait_counts_towards_total_deadline(native):
    provider = runtime.NativeRuntimeProvider("codex", "codex-queue-timeout", None, 1)
    with provider._lock:
        with pytest.raises(runtime.RuntimeConnectionError) as exc:
            provider.complete_text("prompt")
    assert exc.value.code == "timeout"
    assert not native["processes"]


def test_missing_runtime_is_reported_without_starting_process(monkeypatch):
    monkeypatch.setattr(runtime, "_find_cli", lambda kind: None)
    assert runtime.get_runtime_status("codex")["error"] == "not_installed"


def test_codex_config_disables_inherited_mcp_by_name_without_copying_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.external]\ncommand="npx"\nenv={TOKEN="secret-fixture"}\n', encoding="utf-8"
    )
    settings = runtime._codex_config()
    assert settings["mcp_servers.external.enabled"] is False
    assert settings["features.plugins"] is False
    assert settings["features.hooks"] is False
    assert "secret-fixture" not in json.dumps(settings)


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_complete_preserves_messages_and_validates_actual_output_without_repair(native, kind):
    provider = runtime.create_runtime_provider(kind, f"{kind}-complete-contract")
    messages = [
        {"role": "system", "content": "Use only the synthetic fixture."},
        {"role": "user", "content": "Return ok true."},
    ]
    schema = {
        "type": "object",
        "properties": {"ok": {"const": True}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    assert provider.complete(messages, output_schema=schema) == {"ok": True}
    schema["properties"]["ok"] = {"const": False}
    before = len(native["calls"])
    with pytest.raises(ValueError, match="schema"):
        provider.complete(messages, output_schema=schema)
    # Auth probe plus a single completion on Claude; a single app-server on Codex.
    assert len(native["calls"]) - before == (2 if kind == "claude" else 1)
    assert all(process.poll() is not None for process in native["processes"])


def test_runtime_schema_keeps_strict_simple_objects_direct_and_original_unchanged():
    from copy import deepcopy

    from app.ai.runtime_types import normalize_runtime_schema

    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    before = deepcopy(schema)
    transport, wrapped = normalize_runtime_schema(schema)
    assert not wrapped and transport == schema and transport is not schema
    transport["properties"]["ok"]["description"] = "Transport mutation"
    assert schema == before


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_plan_schema_uses_one_envelope_call_preserving_maps_null_and_optional_defaults(
    monkeypatch, kind
):
    from copy import deepcopy

    from app.ai.connections import JSON_SCHEMAS
    PLAN_SCHEMA = JSON_SCHEMAS["parse"]

    provider = runtime.create_runtime_provider(kind, f"{kind}-pydantic-wire")
    value = {
        "stages": [
            {
                "origin": "Torino",
                "destination": "Matera",
                "arrive_by": None,
            }
        ],
        "date": "2026-12-01",
        "data_detta": True,
        "max_budget": None,
    }
    calls = []

    def complete(messages, model, max_tokens, schema):
        calls.append((messages, schema))
        return json.dumps({"json": json.dumps(value)}), None

    monkeypatch.setattr(provider, "_complete", complete)
    original_schema = deepcopy(PLAN_SCHEMA)
    original_messages = [{"role": "user", "content": "Da Torino a Matera senza tetto di spesa"}]
    result = provider.complete(original_messages, output_schema=PLAN_SCHEMA)
    assert result == value
    assert len(calls) == 1
    sent_messages, sent_schema = calls[0]
    assert sent_schema == {
        "type": "object",
        "properties": {"json": {"type": "string"}},
        "required": ["json"],
        "additionalProperties": False,
    }
    assert sent_messages[0] == original_messages[0]
    assert len(original_messages) == 1 and PLAN_SCHEMA == original_schema
    assert json.dumps(PLAN_SCHEMA, ensure_ascii=False) in sent_messages[-1]["content"]
    # Defaults still belong to the original Pydantic contract, not a rewritten
    # transport that forces omitted, non-nullable defaults to become null.
    assert result["max_budget"] is None
    assert "stay_days" not in result["stages"][0]


@pytest.mark.parametrize("mode", ["constraint", "malformed_inner", "invalid_envelope"])
def test_envelope_failure_remains_local_without_another_inference(monkeypatch, mode):
    from app.ai.connections import JSON_SCHEMAS
    PLAN_SCHEMA = JSON_SCHEMAS["parse"]

    provider = runtime.create_runtime_provider("codex", "schema-invalid-fixture")
    calls = []

    def complete(*args):
        calls.append(1)
        if mode == "invalid_envelope":
            return '{"json": {"stages": []}}', None
        if mode == "malformed_inner":
            return json.dumps({"json": '{"stages": ['}), None
        value = {"stages": [], "date": "2026-12-01", "data_detta": "yes"}
        return json.dumps({"json": json.dumps(value)}), None

    monkeypatch.setattr(provider, "_complete", complete)
    with pytest.raises((runtime.RuntimeConnectionError, ValueError)):
        provider.complete([{"role": "user", "content": "synthetic"}], output_schema=PLAN_SCHEMA)
    assert calls == [1]


@pytest.mark.parametrize("form", ["text", "strict_json", "envelope_json"])
def test_codex_turn_pins_low_effort_for_every_response_form(native, monkeypatch, form):
    observed = []
    original = runtime._CodexSession.request

    def request(session, method, params):
        if method == "turn/start":
            observed.append(params.get("effort"))
        return original(session, method, params)

    monkeypatch.setattr(runtime._CodexSession, "request", request)
    provider = runtime.create_runtime_provider("codex", f"low-effort-{form}")
    if form == "text":
        assert provider.complete_text("Synthetic text") == "Risposta sintetica"
    else:
        schema = (
            {"type": "object", "additionalProperties": True}
            if form == "envelope_json"
            else {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            }
        )
        assert isinstance(
            provider.complete(
                [{"role": "user", "content": "Synthetic JSON"}], output_schema=schema
            ),
            dict,
        )
    assert observed == ["low"]


def test_real_trip_schema_prefers_envelope_without_narrowing_or_mutating_original():
    from copy import deepcopy

    from app.ai.connections import JSON_SCHEMAS
    from app.ai.runtime_types import normalize_runtime_schema, validate_output
    schema = JSON_SCHEMAS["parse"]
    before = deepcopy(schema)
    wire, wrapped = normalize_runtime_schema(schema)
    assert wrapped and wire["properties"] == {"json": {"type": "string"}}
    assert schema == before
    valid = {
        "stages": [{"origin": "Torino", "destination": "Roma", "stay_days": 3},
                   {"origin": "Roma", "destination": "Matera"}],
        "date": "2026-12-01", "data_detta": True,
    }
    validate_output(valid, schema)
    invalid = deepcopy(valid)
    invalid["stages"][0]["stay_days"] = -1
    with pytest.raises(ValueError, match="schema"):
        validate_output(invalid, schema)
