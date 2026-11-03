"""Exercise the real Codex CLI against one synthetic Trip Finder installation.

The app must already be open. Native authentication is reused; global assistant
configuration is ignored, never edited. No transport search or inference is
performed by Trip Finder's tools in this check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import re
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.runtime import _find_cli, _child_env


def _personal_configuration() -> tuple[Path, str, set[str]]:
    """Inspect server names only; no command, env, argument or secret leaves here."""
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = home / "config.toml"
    try:
        raw = path.read_bytes()
        configuration = tomllib.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("Configurazione Codex globale non leggibile") from None
    servers = set()
    for table in [configuration, *configuration.get("profiles", {}).values()]:
        if not isinstance(table, dict):
            continue
        names = table.get("mcp_servers", {})
        if not isinstance(names, dict):
            raise RuntimeError("Inventario MCP globale non valido")
        for name in names:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise RuntimeError("Nome server MCP globale non supportato dal collaudo")
            servers.add(name)
    if "tripfinder" not in servers:
        raise RuntimeError("Il server tripfinder non è registrato nella configurazione globale")
    return path, hashlib.sha256(raw).hexdigest(), servers


def verify(state: Path | None, output: Path, *, expect_closed: bool = False, runtime: Path | None = None,
           personal: bool = False) -> dict:
    configuration_path = None
    configuration_digest = None
    server_names: set[str] = set()
    entry = ""
    if personal:
        if expect_closed or runtime:
            raise ValueError("Il collaudo personale usa l'app aperta e la sola configurazione globale")
        configuration_path, configuration_digest, server_names = _personal_configuration()
    else:
        if state is None:
            raise ValueError("Specificare --state per il collaudo sintetico")
        descriptor_path = state / ("runtime-closed.json" if expect_closed and (state / "runtime-closed.json").exists() else "runtime.json")
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        token_file = Path(descriptor["token_path"])
        # Only a path is passed to the CLI, never the capability itself.
        if runtime:
            command, args = str(runtime / "TripFinder.exe"), ["--mcp-stdio"]
        else:
            command = sys.executable
            args = [str(Path(__file__).resolve().parents[1] / "app" / "mcp_server.py"), "--stdio"]
        args += ["--token-file", str(token_file), "--runtime-file", str(descriptor_path),
                 "--instance-id", descriptor["instance_id"]]
        entry = ("{ command = " + json.dumps(command) + ", args = " + json.dumps(args)
                 + ", enabled = true, required = true, startup_timeout_sec = 25, tool_timeout_sec = 30, "
                 + 'enabled_tools = ["tripfinder_get_context", "tripfinder_list_providers"] }')
    executable = _find_cli("codex")
    if not executable:
        raise RuntimeError("Codex CLI non installata")
    prompt = ("Collaudo sintetico Trip Finder. Usa obbligatoriamente il tool MCP "
              "tripfinder_get_context, poi tripfinder_list_providers. Non cercare viaggi, "
              "non usare shell, non modificare dati. Rispondi solo con il numero di "
              "operatori restituiti e se il profilo è leggibile.")
    if expect_closed:
        prompt = ("Collaudo sintetico Trip Finder chiuso. Chiama obbligatoriamente "
                  "tripfinder_get_context una volta. Non usare shell o altri tool. "
                  "Riporta il codice di errore e spiega che bisogna aprire l'app.")
    if personal:
        prompt = ("Collaudo del collegamento globale Trip Finder già installato. Chiama obbligatoriamente "
                  "e soltanto il tool tripfinder_list_tessere una volta, per leggere il catalogo pubblico. "
                  "Non usare altri tool, shell, profilo, contesto, ricerche, dati o documenti personali. "
                  "Non modificare nulla. Rispondi soltanto con il numero di voci del catalogo pubblico.")
    with tempfile.TemporaryDirectory(prefix="tripfinder-external-client-") as cwd:
        argv = [str(executable), "--no-daemon", "exec"]
        if not personal:
            argv.append("--ignore-user-config")
        argv += ["--ignore-rules",
                "--ephemeral", "--skip-git-repo-check", "--json", "--color", "never", "-C", cwd,
                "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
                "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
                "-c", 'model_reasoning_effort="low"', "-c", 'web_search="disabled"']
        if personal:
            for name in sorted(server_names - {"tripfinder"}):
                argv += ["-c", f"mcp_servers.{name}.enabled=false"]
            argv += ["-c", "mcp_servers.tripfinder.enabled=true", "-c", "mcp_servers.tripfinder.required=true",
                     "-c", 'mcp_servers.tripfinder.enabled_tools=["tripfinder_list_tessere"]']
        else:
            argv += ["-c", "mcp_servers.tripfinder=" + entry]
        for feature in ["shell_tool", "unified_exec", "multi_agent", "multi_agent_v2",
                        "apps", "plugins", "hooks", "memories", "computer_use", "skill_search"]:
            argv += ["-c", f"features.{feature}=false"]
        argv += ["-c", "features.skip_host_skill_discovery=true", "-c", "project_doc_max_bytes=0", "-"]
        try:
            result = subprocess.run(argv, input=prompt, env=_child_env(), cwd=cwd,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
        except (subprocess.TimeoutExpired, OSError) as exc:
            partial = getattr(exc, "stdout", "") or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            result = subprocess.CompletedProcess(argv, -1, partial, type(exc).__name__)
    events = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict): events.append(item)
        except ValueError:
            continue
    calls = []
    answers = []
    for event in events:
        item = event.get("item", {})
        if item.get("type") == "mcp_tool_call" and event.get("type") == "item.completed":
            tool_result = item.get("result") or {}
            calls.append({"tool": item.get("tool"), "server": item.get("server"),
                          "status": item.get("status"), "error": item.get("error"),
                          "is_error": tool_result.get("is_error", tool_result.get("isError", False))})
        if item.get("type") == "agent_message" and event.get("type") == "item.completed":
            answers.append(item.get("text", ""))
    names = {call["tool"] for call in calls if call["server"] == "tripfinder"}
    required = ({"tripfinder_list_tessere"} if personal else
                {"tripfinder_get_context"} if expect_closed else {"tripfinder_get_context", "tripfinder_list_providers"})
    closed_detected = "tripfinder_unreachable_open_the_local_app" in result.stdout
    modified = False
    if configuration_path is not None:
        try:
            modified = hashlib.sha256(configuration_path.read_bytes()).hexdigest() != configuration_digest
        except OSError:
            modified = True
    success = result.returncode == 0 and required <= names and (closed_detected if expect_closed else all(c["status"] == "completed" and not c["is_error"] for c in calls))
    if personal:
        success = success and not modified and names == required and len(calls) == 1 and all(c["server"] == "tripfinder" for c in calls)
    report = {"ok": success, "client": "Codex CLI", "actual_cli": str(executable),
              "external_cwd": True, "global_config_modified": modified, "expect_closed": expect_closed,
              "exit_code": result.returncode, "calls": calls, "closed_detected": closed_detected,
              "answer": answers[-1] if answers else "", "event_types": sorted({e.get("type", "") for e in events})}
    if not success:
        # With user config loaded, stderr could quote a broken config value.
        # Keep only controlled metadata in the personal report.
        report["diagnostic"] = "global_client_probe_failed" if personal else result.stderr[-1800:]
    if personal:
        report.update(personal=True, configuration_source="existing_global_entry",
                      other_servers_disabled_for_probe=True, global_configuration_unchanged=not modified,
                      tool_allowlist=["tripfinder_list_tessere"], personal_data_tools_enabled=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--expect-closed", action="store_true")
    parser.add_argument("--personal", action="store_true", help="Use existing global registration; public catalogue tool only")
    options = parser.parse_args()
    try:
        report = verify(options.state, options.output, expect_closed=options.expect_closed, runtime=options.runtime,
                        personal=options.personal)
        print(json.dumps({"ok": report["ok"], "calls": report["calls"], "output": str(options.output)}, ensure_ascii=True))
        raise SystemExit(0 if report["ok"] else 1)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(1)
