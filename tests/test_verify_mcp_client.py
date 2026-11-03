"""Personal global-client proof never injects a server or reads personal data."""
import json
from types import SimpleNamespace

import pytest

from scripts import verify_mcp_client as probe


@pytest.fixture
def global_config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.jobfinder]\ncommand="private-existing-command"\n'
                    '[mcp_servers.tripfinder]\ncommand="installed-tripfinder"\n'
                    'args=["--mcp-stdio", "private-token-path"]\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(probe, "_find_cli", lambda kind: "native-codex.exe")
    monkeypatch.setattr(probe, "_child_env", dict)
    return path


def completion(tool="tripfinder_list_tessere"):
    return SimpleNamespace(returncode=0, stderr="private-config-value-must-not-appear", stdout="\n".join(map(json.dumps, [
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "tripfinder", "tool": tool,
                                                "status": "completed", "result": {"isError": False}}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "13 voci del catalogo pubblico."}},
    ])))


def test_personal_uses_global_entry_and_disables_other_servers_only_in_argv(global_config, monkeypatch, tmp_path):
    calls = []
    before = global_config.read_bytes()
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return completion()
    monkeypatch.setattr(probe.subprocess, "run", run)
    output = tmp_path / "report.json"
    report = probe.verify(None, output, personal=True)
    assert report["ok"] and report["global_configuration_unchanged"]
    argv, request = calls[0]
    assert "--no-daemon" in argv and "--ignore-user-config" not in argv
    assert "mcp_servers.jobfinder.enabled=false" in argv
    assert "mcp_servers.tripfinder.required=true" in argv
    assert 'mcp_servers.tripfinder.enabled_tools=["tripfinder_list_tessere"]' in argv
    assert not any(arg.startswith("mcp_servers.tripfinder=") for arg in argv)
    assert "private-existing-command" not in str(argv)
    assert "private-token-path" not in str(argv)
    assert "tripfinder_get_context" not in request["input"]
    assert global_config.read_bytes() == before
    assert "private-" not in output.read_text(encoding="utf-8")


def test_personal_requires_existing_registration_before_starting_cli(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[mcp_servers.jobfinder]\ncommand="existing"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: pytest.fail("CLI must not start"))
    with pytest.raises(RuntimeError, match="non è registrato"):
        probe.verify(None, tmp_path / "report.json", personal=True)


def test_personal_rejects_unrequested_context_tool_and_sanitizes_diagnostic(global_config, monkeypatch, tmp_path):
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: completion("tripfinder_get_context"))
    output = tmp_path / "report.json"
    report = probe.verify(None, output, personal=True)
    assert not report["ok"]
    assert report["diagnostic"] == "global_client_probe_failed"
    assert "private-config" not in output.read_text(encoding="utf-8")


def test_personal_reports_configuration_mutation_as_failure(global_config, monkeypatch, tmp_path):
    def run(*args, **kwargs):
        global_config.write_text(global_config.read_text() + "\n# changed by synthetic fixture\n", encoding="utf-8")
        return completion()
    monkeypatch.setattr(probe.subprocess, "run", run)
    report = probe.verify(None, tmp_path / "report.json", personal=True)
    assert not report["ok"] and report["global_config_modified"]


def test_personal_timeout_still_checks_global_configuration_and_writes_safe_report(global_config, monkeypatch, tmp_path):
    def run(*args, **kwargs):
        raise probe.subprocess.TimeoutExpired("private-command", 150)
    monkeypatch.setattr(probe.subprocess, "run", run)
    output = tmp_path / "report.json"
    report = probe.verify(None, output, personal=True)
    assert not report["ok"] and report["global_configuration_unchanged"]
    assert report["diagnostic"] == "global_client_probe_failed"
    assert "private-command" not in output.read_text(encoding="utf-8")
