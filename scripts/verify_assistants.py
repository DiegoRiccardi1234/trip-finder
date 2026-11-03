"""Live Codex/browser verification in a synthetic, isolated Trip Finder store.

Runs six bounded native completions: three parses, one advice, two chat turns.
No model retry, personal archive, API key, purchase or booking is involved.
Optional external Codex MCP checks run separately against the same app/store.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import _bootstrap  # noqa: F401
import httpx

from app.models import CostBreakdown, CostLine, Fare, Itinerary, Leg, Mode, Node, NodeKind
from app.version import VERSION

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8099"
PUBLIC_DATA = ("stations.csv", "airports.csv", "cities15000.txt", "tessere.json")


class VerificationFailure(RuntimeError):
    pass


def _environment(workspace: Path) -> dict[str, str]:
    env = {}
    for key, value in os.environ.items():
        name = key.upper()
        if name in {"CODEX_HOME", "CLAUDE_CONFIG_DIR"}:
            env[key] = value
        elif not (name.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_", "CLAUDE_"))
                  or any(part in name for part in ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "PASSWORD", "SECRET"))):
            env[key] = value
    env.update(TRIPFINDER_WORKSPACE=str(workspace), TRIPFINDER_PORT="8099",
               ALLOW_PAID_PROVIDERS="false", LLM_PROVIDER="", LLM_BASE_URL="",
               SEARCH_TIME_BUDGET="65", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def _synthetic(target: date) -> tuple[dict, dict]:
    origin = Node(id="synthetic:torino", name="Torino sintetica", kind=NodeKind.STATION,
                  lat=45.06, lon=7.67, country="IT", city="Torino", timezone="Europe/Rome")
    destination = Node(id="synthetic:matera", name="Matera sintetica", kind=NodeKind.STATION,
                       lat=40.66, lon=16.60, country="IT", city="Matera", timezone="Europe/Rome")
    itineraries, options = [], []
    for index, (hour, price, duration) in enumerate(((8, 45.0, 540), (9, 70.0, 480)), 1):
        depart = datetime.combine(target, datetime.min.time(), timezone.utc) + timedelta(hours=hour)
        arrive = depart + timedelta(minutes=duration)
        leg = Leg(provider="synthetic", operator="Operatore sintetico", mode=Mode.RAIL,
                  origin=origin, destination=destination, depart=depart, arrive=arrive,
                  fare=Fare(amount=price), notes=["Dati sintetici di collaudo, nessuna prenotazione"])
        itinerary = Itinerary(id=f"synthetic-{index}", legs=[leg],
                              cost=CostBreakdown(lines=[CostLine(label="Tariffa sintetica", amount=price, kind="fare")]))
        itineraries.append(itinerary.model_dump(mode="json"))
        options.append({"ref": str(index), "depart": depart.isoformat(), "arrive": arrive.isoformat(),
                        "duration_min": duration, "total": price, "modes": ["rail"],
                        "operators": ["Operatore sintetico"], "n_changes": 0, "n_tickets": 1,
                        "notes": ["Dati sintetici di collaudo"]})
    query = {"origin": "Torino", "destination": "Matera", "date": target.isoformat(), "modes": ["rail"], "max_changes": 3}
    advice = {**query, "options": options, "found": 2, "pax": 1, "raw_text": "Confronta questi due itinerari sintetici, senza cercare altri dati."}
    return {"query": query, "itineraries": itineraries}, advice


async def run(args: argparse.Namespace) -> int:
    with socket.socket() as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 8099))
    workspace = Path(tempfile.mkdtemp(prefix="tripfinder-assistants-"))
    data = workspace / "data"
    data.mkdir()
    for name in PUBLIC_DATA:
        source = ROOT / "data" / name
        if source.is_file():
            shutil.copy2(source, data / name)
        elif name != "tessere.json":
            raise VerificationFailure("missing_public_catalogue:" + name)
    (workspace / ".env").write_text("ALLOW_PAID_PROVIDERS=false\nLLM_PROVIDER=\n", encoding="utf-8")
    (data / "local_secrets.json").write_text("{}\n", encoding="utf-8")
    report: dict = {"version": VERSION, "checked_at": datetime.now(timezone.utc).isoformat(),
                    "mode": "runtime" if args.runtime else "source", "workspace": str(workspace),
                    "state_dir": str(data), "runtime_root": str(args.runtime.resolve().parent) if args.runtime else None,
                    "workspace_retained": args.keep_workspace, "cases": [], "native_completions": 0, "native_attempts": 0,
                    "max_native_completions": 10, "automatic_retries": False,
                    "personal_data_used": False, "paid_api_fallback": False, "port": 8099}
    target = date.today() + timedelta(days=14)
    context, advice = _synthetic(target)
    process = None
    server_log = (workspace / "server.log").open("w", encoding="utf-8")
    browser = None
    playwright = None

    def save() -> None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def record(name: str, payload: dict | list | None = None, *, native: bool = False) -> None:
        if native:
            report["native_completions"] += 1
        report["cases"].append({"name": name, "ok": True, "result": payload})
        save()
        print(f"{name}: OK (native {report['native_completions']}/10)", flush=True)

    def reserve_native() -> None:
        if report["native_attempts"] >= 10:
            raise VerificationFailure("verification_native_call_limit")
        report["native_attempts"] += 1
        save()

    def checked(response: httpx.Response) -> dict | list:
        payload = response.json()
        if response.status_code >= 400:
            detail = payload.get("detail", "request_failed") if isinstance(payload, dict) else "request_failed"
            raise VerificationFailure(f"HTTP {response.status_code}: {detail}")
        return payload

    try:
        env = _environment(workspace)
        command = ([str(args.runtime.resolve()), "--port", "8099", "--no-browser"] if args.runtime else
                   [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8099", "--no-access-log"])
        process = subprocess.Popen(command, cwd=ROOT if not args.runtime else args.runtime.resolve().parent,
                                   env=env, stdout=server_log, stderr=subprocess.STDOUT,
                                   creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        async with httpx.AsyncClient(base_url=URL, timeout=330, trust_env=False, follow_redirects=False,
                                     headers={"Origin": URL}) as http:
            deadline = time.monotonic() + 100
            while True:
                if process.poll() is not None:
                    raise VerificationFailure("server_exited:" + str(process.returncode))
                try:
                    response = await http.get("/api/runtime", timeout=2)
                    if response.status_code == 200:
                        descriptor = json.loads((data / "runtime.json").read_text(encoding="utf-8"))
                        identity = response.json()
                        if identity["instance_id"] != descriptor["instance_id"]:
                            raise VerificationFailure("wrong_installation_on_8099")
                        assert identity["version"] == VERSION
                        break
                except (httpx.HTTPError, OSError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise VerificationFailure("server_startup_timeout")
                await asyncio.sleep(0.25)
            bootstrap = checked(await http.post("/api/agent/ui/session", headers={"x-tripfinder-ui": "1"}))
            http.headers["x-tripfinder-csrf"] = bootstrap["csrf_token"]
            snapshot = checked(await http.get("/api/agent/connections"))
            assert all(not snapshot["routing"][task]["primary"] for task in ("parse", "planning", "advice", "chat"))
            assert not next(row for row in snapshot["connections"] if row["id"] == "codex")["enabled"]
            record("public_defaults_disabled")
            status = checked(await http.get("/api/agent/runtimes/codex"))
            record("native_account_probe", {key: status.get(key) for key in ("installed", "authenticated", "version", "error")})
            if not status.get("authenticated"):
                raise VerificationFailure("runtime_unavailable:" + str(status.get("error")))

            from patchright.async_api import async_playwright
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=args.headless)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(URL, wait_until="domcontentloaded")
            await page.get_by_role("heading", name="Trip Finder", exact=True).wait_for()
            await page.get_by_role("button", name="Impostazioni", exact=True).click()
            codex = page.locator(".connection-row").filter(has=page.get_by_text("Codex CLI", exact=True))
            await codex.get_by_role("button", name="Modifica", exact=True).click()
            await page.locator('#agent-connection-form [name="enabled"]').check()
            async with page.expect_response(lambda r: r.url.endswith("/api/agent/connections") and r.request.method == "POST") as pending:
                await page.get_by_role("button", name="Salva connessione", exact=True).click()
            assert (await pending.value).status == 200
            await page.wait_for_function("document.querySelector('#agent-settings-state').textContent.includes('Connessione salvata')")
            for task in ("parse", "planning", "advice", "chat"):
                await page.locator(f"#agent-route-{task}").select_option("codex")
            async with page.expect_response(lambda r: r.url.endswith("/api/agent/routing") and r.request.method == "PUT") as pending:
                await page.get_by_role("button", name="Salva attività", exact=True).click()
            assert (await pending.value).status == 200
            routes = checked(await http.get("/api/agent/connections"))["routing"]
            assert all(route == {"primary": "codex", "fallbacks": []} for route in routes.values())
            record("browser_settings_codex_only", routes)

            await page.get_by_role("button", name="Ricerca", exact=True).click()
            await page.locator('[name="nl"]').fill("da Torino a Matera")
            reserve_native()
            async with page.expect_response(lambda r: r.url.endswith("/api/parse"), timeout=330000) as pending:
                await page.get_by_role("button", name="Interpreta", exact=True).click()
            response = await pending.value
            parsed = await response.json()
            assert response.status == 200 and parsed.get("domanda") == "Per quando?", parsed
            await page.locator("#nl-feedback").filter(has_text="Per quando?").wait_for()
            record("browser_parse_missing_date", parsed, native=True)
            await page.locator('[name="nl"]').fill("il " + target.strftime("%d/%m/%Y"))
            reserve_native()
            async with page.expect_response(lambda r: r.url.endswith("/api/parse"), timeout=330000) as pending:
                await page.get_by_role("button", name="Interpreta", exact=True).click()
            response = await pending.value
            parsed = await response.json()
            assert response.status == 200 and parsed["stages"][0]["date"] == target.isoformat(), parsed
            await page.wait_for_function("document.querySelector('#nl-feedback').classList.contains('ok')")
            assert (await page.locator('[name="origin"]').input_value()).lower() == "torino"
            assert (await page.locator('[name="destination"]').input_value()).lower() == "matera"
            record("browser_parse_second_turn", parsed, native=True)

            reserve_native()
            parsed = checked(await http.post("/api/parse", json={"text": f"da Torino a Roma il {target:%d/%m/%Y}, resto tre giorni, poi vado a Matera"}))
            assert len(parsed["stages"]) == 2 and parsed["stages"][0]["stay_days"] == 3
            assert parsed["stages"][1]["date"] == (target + timedelta(days=3)).isoformat()
            record("parse_multiple_stages", parsed, native=True)
            reserve_native()
            answer = checked(await http.post("/api/advice", json=advice))
            assert answer.get("text") and not answer.get("reason"), answer
            record("advice_synthetic_itineraries", answer, native=True)

            # The real UI/backend is used; only the optional call budget is
            # lowered to one. No API response is mocked or substituted.
            async def chat_budget(route):
                body = route.request.post_data_json
                body["max_calls"] = 1
                await route.continue_(post_data=json.dumps(body))
            await page.route("**/api/agent/conversations/*/messages", chat_budget)
            await page.get_by_role("button", name="Chat viaggi", exact=True).click()
            await page.locator("#agent-input").fill("Vorrei andare da Torino a Matera ma non ho deciso quando. Rispondi direttamente chiedendomi la data, senza strumenti o ricerche.")
            reserve_native()
            async with page.expect_response(lambda r: "/messages" in r.url and r.request.method == "POST", timeout=330000) as pending:
                await page.get_by_role("button", name="Invia", exact=True).click()
            response = await pending.value
            chat = await response.json()
            assert response.status == 200, chat
            await page.locator(".assistant-message--assistant").wait_for()
            assert len(chat["conversation"]["messages"]) == 2
            record("browser_chat_from_zero", chat, native=True)
            screenshot = args.output.with_suffix(".png")
            await page.screenshot(path=str(screenshot), full_page=True)
            report["browser_screenshot"] = str(screenshot)

            imported = checked(await http.post("/api/agent/conversations", json={"title": "Collaudo sintetico risultati", "context": context}))
            conversation_id = imported["id"]
            reserve_native()
            chat = checked(await http.post(f"/api/agent/conversations/{conversation_id}/messages", json={
                "text": "Usando soltanto gli itinerari nel contesto, rispondi direttamente senza strumenti: quale costa meno e quanto costa? Sono dati sintetici.",
                "request_id": uuid.uuid4().hex, "max_calls": 1}))
            last = chat["conversation"]["messages"][-1]["content"]
            assert "45" in last, last
            record("chat_from_imported_results", chat, native=True)

            operation_id = imported["context"]["operation_id"]
            operation = checked(await http.get(f"/api/agent/operations/{operation_id}"))
            itinerary_id = operation["result"]["stages"][0]["itineraries"][0]["id"]
            proposal = checked(await http.post("/api/agent/proposals/save-itinerary", json={
                "operation_id": operation_id, "itinerary_id": itinerary_id,
                "name": "Itinerario sintetico verificato", "request_id": uuid.uuid4().hex}))
            assert not checked(await http.get("/api/agent/saved-itineraries"))["itineraries"]
            await page.get_by_role("button", name="Profilo", exact=True).click()
            card = page.locator(".assistant-proposal").filter(has_text="Itinerario sintetico verificato")
            async with page.expect_response(lambda r: "/proposals/" in r.url and r.url.endswith("/confirm")) as pending:
                await card.get_by_role("button", name="Conferma", exact=True).click()
            assert (await pending.value).status == 200
            saved = checked(await http.get("/api/agent/saved-itineraries"))["itineraries"]
            assert len(saved) == 1
            historic = checked(await http.get("/api/agent/saved-itineraries/" + saved[0]["id"]))
            assert historic["historical"] and historic["snapshot"]["cost"]["total"] == 45
            record("browser_confirm_historical_snapshot", {"historical": True, "total": 45, "count": len(saved)})
            assert not errors, errors
            record("browser_no_javascript_errors")

            if args.search:
                plan = {"stages": [{"origin": "Torino", "destination": "Matera", "date": target.isoformat()}],
                        "modes": ["rail", "bus"], "allow_night": False}
                started = checked(await http.post("/api/agent/operations", json={"plan": plan, "request_id": uuid.uuid4().hex}))
                deadline = time.monotonic() + 100
                while True:
                    current = checked(await http.get("/api/agent/operations/" + started["operation_id"]))
                    if current["status"] not in {"queued", "running"}:
                        break
                    if time.monotonic() > deadline:
                        raise VerificationFailure("search_operation_timeout")
                    await asyncio.sleep(1)
                results = current["result"]
                total = sum(len(stage.get("itineraries", [])) for stage in results.get("stages", []))
                assert current["status"] == "completed"
                record("one_live_operator_search", {"status": current["status"], "itineraries": total,
                                                     "query": plan, "ai_hubs": False})
            report["app_validation_ok"] = True
            if args.mcp_cli:
                # Registering the runtime descriptor does not authorize an
                # external client yet: setup creates its separate capability.
                await page.get_by_role("button", name="Impostazioni", exact=True).click()
                async with page.expect_response(lambda r: r.url.endswith("/api/agent/setup") and r.request.method == "POST") as pending:
                    await page.get_by_role("button", name="Mostra configurazione MCP", exact=True).click()
                assert (await pending.value).status == 200
                record("browser_mcp_setup", {"capability_file_created": (data / "agent" / "mcp-token.txt").is_file(),
                                              "setup_contains_paths_only": True})
                from scripts.verify_mcp_client import verify
                mcp = await asyncio.to_thread(verify, data, args.output.with_name(args.output.stem + "-mcp-open.json"),
                                              runtime=args.runtime.resolve().parent if args.runtime else None)
                assert mcp["ok"], mcp
                record("actual_codex_cli_mcp_open", {"ok": mcp["ok"], "calls": mcp["calls"]})
            if (data / "runtime.json").exists():
                shutil.copy2(data / "runtime.json", data / "runtime-closed.json")
            report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        print("FAIL: " + str(exc)[:500], flush=True)
    finally:
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
        if process is not None and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server_log.close()
        report["server_stopped"] = True
        if report.get("ok") and args.mcp_cli:
            try:
                from scripts.verify_mcp_client import verify
                mcp = await asyncio.to_thread(verify, data, args.output.with_name(args.output.stem + "-mcp-closed.json"),
                                              expect_closed=True, runtime=args.runtime.resolve().parent if args.runtime else None)
                assert mcp["ok"], mcp
                record("actual_codex_cli_mcp_closed", {"ok": mcp["ok"], "calls": mcp["calls"]})
            except Exception as exc:
                report["ok"] = False
                report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:1500]}
        db_path = data / "trip_finder.db"
        if db_path.exists():
            with closing(sqlite3.connect(db_path)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                report["synthetic_state_counts"] = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                                   for table in ("saved_itineraries", "agent_conversations", "ai_routes") if table in tables}
        report["no_real_keys"] = json.loads((data / "local_secrets.json").read_text(encoding="utf-8")) == {}
        save()
        if not args.keep_workspace:
            shutil.rmtree(workspace)
    print(json.dumps({"ok": report["ok"], "native_completions": report["native_completions"], "output": str(args.output),
                      "state_dir": str(data) if args.keep_workspace else None}, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, help="Bundle launcher TripFinder.exe; source server otherwise")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--mcp-cli", action="store_true")
    parser.add_argument("--search", action="store_true", help="One real, read-only operator search")
    parser.add_argument("--headless", action="store_true", help="Browser without a visible window")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
