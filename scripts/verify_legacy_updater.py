"""Run a historical compiled updater on synthetic state, then verify its restart.

--runtime is an extracted historical release or an installed runtime. Only EXEs
and _internal are copied; no personal data or configuration is read/copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from scripts.updater import _vivo
from app.install_layout import state_dir
from app.update_sync import claim_update_lock


def verify(runtime: Path, archive: Path, output: Path, organized_template: Path | None = None) -> dict:
    if os.name != "nt":
        raise RuntimeError("Il collaudo dell'updater compilato richiede Windows")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 8099))
    workspace = Path(tempfile.mkdtemp(prefix="tripfinder-historical-"))
    old_runner, target = workspace / "old-updater", workspace / "installed"
    old_runner.mkdir()
    shutil.copy2(runtime / "Aggiorna.exe", old_runner / "Aggiorna.exe")
    shutil.copytree(runtime / "_internal", old_runner / "_internal")
    if (runtime / "TripFinder.exe").exists():
        shutil.copy2(runtime / "TripFinder.exe", old_runner / "TripFinder.exe")
    if organized_template is not None:
        template = organized_template.resolve()
        shutil.copytree(template, target, ignore=lambda directory, names: {"state"} & set(names) if Path(directory).resolve() == template else set())
    else:
        # Exercise an existing historical program tree, including old DLLs,
        # rather than making the old updater populate an empty program folder.
        target.mkdir()
        shutil.copy2(old_runner / "Aggiorna.exe", target / "Aggiorna.exe")
        if (old_runner / "TripFinder.exe").exists():
            shutil.copy2(old_runner / "TripFinder.exe", target / "TripFinder.exe")
        shutil.copytree(old_runner / "_internal", target / "_internal")
    data = state_dir(target)
    data.mkdir(parents=True, exist_ok=True)
    relative_data = data.relative_to(target).as_posix()
    env_name = relative_data + "/.env" if organized_template is not None else ".env"
    vendor_name = "app/vendor/custom.json" if organized_template is not None else "vendor/custom.json"
    (target / vendor_name).parent.mkdir(parents=True, exist_ok=True)
    markers = {env_name: "ALLOW_PAID_PROVIDERS=false\n", relative_data + "/local_secrets.json": "{}\n", relative_data + "/profile-marker.txt": "preserve\n", vendor_name: '{"synthetic":"preserve"}\n'}
    for name, value in markers.items():
        (target / name).write_text(value, encoding="utf-8")
    with closing(sqlite3.connect(data / "trip_finder.db")) as database:
        database.execute("CREATE TABLE smoke_marker(value TEXT)")
        database.execute("INSERT INTO smoke_marker VALUES ('preserve')")
        database.commit()
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY") and key not in {"LLM_PROVIDER", "LLM_BASE_URL"}}
    env.update(TRIPFINDER_WORKSPACE=str(target), TRIPFINDER_PORT="8099", TRIPFINDER_NO_BROWSER="1", ALLOW_PAID_PROVIDERS="false")
    launched_pid = None
    updater = None
    report = {"workspace": str(workspace), "historical_updater": str(runtime / "Aggiorna.exe"), "archive": str(archive),
              "updater_sha256": hashlib.sha256((runtime / "Aggiorna.exe").read_bytes()).hexdigest(),
              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "organized_target": organized_template is not None}
    launcher_hash = hashlib.sha256((target / "TripFinder.exe").read_bytes()).hexdigest() if organized_template is not None else None
    try:
        command = [str(old_runner / "Aggiorna.exe"), "--zip", str(archive.resolve()), "--dest", str(target), "--exe", str(target / "TripFinder.exe"), "--pid", "0"]
        if organized_template is not None:
            owner = claim_update_lock(data / "aggiornamento.lock", "compiled-organized-verifier")
            command.extend(["--lock-owner", owner, "--port", "8099"])
        updater = subprocess.Popen(command, cwd=old_runner, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        assert updater.wait(timeout=180) == 0, "Updater storico fallito"
        deadline = time.monotonic() + 120
        with httpx.Client(base_url="http://127.0.0.1:8099", timeout=5, trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    descriptor = json.loads((data / "runtime.json").read_text(encoding="utf-8"))
                    response = http.get("/api/runtime")
                    response.raise_for_status()
                    assert response.json()["instance_id"] == descriptor["instance_id"]
                    assert descriptor["base_url"].rstrip("/") == "http://127.0.0.1:8099"
                    launched_pid = int(descriptor["pid"])
                    report.update(restarted=True, version=response.json().get("version"), effective_port=8099)
                    break
                except (OSError, ValueError, KeyError, httpx.HTTPError):
                    time.sleep(0.5)
            else:
                raise RuntimeError("La nuova app non si è riavviata sulla porta di collaudo")
        assert all((target / name).read_text(encoding="utf-8") == value for name, value in markers.items())
        report["user_files_preserved"] = True
        if organized_template is not None:
            assert hashlib.sha256((target / "TripFinder.exe").read_bytes()).hexdigest() == launcher_hash
            assert not (data / "aggiornamento.lock").exists(), "Updater did not release its own lock"
            assert {entry.name for entry in target.iterdir()} == {"TripFinder.exe", "LEGGIMI.txt", "app", "state"}
            report.update(stable_launcher_preserved=True, owned_lock_released=True, organized_layout_preserved=True)
    finally:
        if updater is not None and updater.poll() is None:
            subprocess.run(["taskkill", "/PID", str(updater.pid), "/T", "/F"], capture_output=True, check=False)
            updater.wait(timeout=15)
        if launched_pid:
            subprocess.run(["taskkill", "/PID", str(launched_pid), "/T", "/F"], capture_output=True, check=False)
            deadline = time.monotonic() + 15
            while _vivo(launched_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not _vivo(launched_pid), "Il processo sintetico non si è fermato"
        else:
            # A startup failure may precede descriptor publication. The path
            # is a unique verifier-owned TEMP tree, never an installed app.
            quoted = str(target / "TripFinder.exe").replace("'", "''")
            quoted_runtime = str(target / "app" / "TripFinder.exe").replace("'", "''")
            command = "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq '" + quoted + "' -or $_.ExecutablePath -eq '" + quoted_runtime + "' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        with closing(sqlite3.connect(data / "trip_finder.db")) as database:
            assert database.execute("SELECT value FROM smoke_marker").fetchone() == ("preserve",)
        report["database_preserved"] = True
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--organized-template", type=Path, help="New updater only: exercise the organized target layout")
    args = parser.parse_args()
    print(json.dumps(verify(args.runtime, args.zip, args.output, args.organized_template), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
