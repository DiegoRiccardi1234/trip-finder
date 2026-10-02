"""Verifica ZIP, aggiornamento e avvio del bundle in un'installazione isolata.

Non tocca l'app installata. Il workspace temporaneo e il report restano
disponibili per ispezione; nessuna chiave reale viene copiata.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import _bootstrap  # noqa: F401
import httpx

from app.update_sync import sincronizza
from app.providers import registry
from app.version import VERSION


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, default=Path("dist/TripFinder-windows.zip"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 8099))  # fallisce se la porta e' occupata
    workspace = Path(tempfile.mkdtemp(prefix="tripfinder-bundle-"))
    extraction = workspace / "new"
    installation = workspace / "installed"
    report = {"version": VERSION, "workspace": str(workspace)}
    with zipfile.ZipFile(args.zip) as archive:
        names = archive.namelist()
        forbidden = {".env", ".env.local", "local_secrets.json", "trip_finder.db"}
        assert not any(forbidden.intersection(Path(name).parts) or "/logs/" in name for name in names), "dati personali nello ZIP"
        assert all(f"TripFinder/data/{name}" in names for name in ("stations.csv", "airports.csv", "cities15000.txt"))
        assert archive.testzip() is None, "ZIP corrotto"
        archive.extractall(extraction)
    (installation / "data").mkdir(parents=True)
    markers = {".env": "# installazione di prova\nALLOW_PAID_PROVIDERS=false\n", "data/local_secrets.json": "{}\n", "data/profile-marker.txt": "preserve\n"}
    for name, content in markers.items():
        (installation / name).write_text(content, encoding="utf-8")
    with sqlite3.connect(installation / "data/trip_finder.db") as db:
        db.execute("CREATE TABLE smoke_marker(value TEXT)")
        db.execute("INSERT INTO smoke_marker VALUES ('preserve')")
    copied = sincronizza(origine=extraction / "TripFinder", destinazione=installation)
    assert all((installation / name).read_text(encoding="utf-8") == content for name, content in markers.items())
    report.update(zip_clean=True, updater_preserves_user_files=True, copied_files=copied)
    env = dict(os.environ)
    for field in list(env):
        if field.endswith("_API_KEY") or field in ("LLM_BASE_URL", "LLM_PROVIDER"):
            env.pop(field)
    env["TRIPFINDER_WORKSPACE"] = str(installation)
    env["ALLOW_PAID_PROVIDERS"] = "false"
    process = subprocess.Popen([str(installation / "TripFinder.exe"), "--port", "8099", "--no-browser"], cwd=installation, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 90
        with httpx.Client(base_url="http://127.0.0.1:8099", timeout=10) as http:
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"bundle terminato: {process.returncode}")
                try:
                    response = http.get("/openapi.json")
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
            assert response.json()["info"]["version"] == VERSION
            assert "Trip Finder" in http.get("/").text
            assert http.get("/api/ai/status").json()["configured"] is False
            providers = http.get("/api/providers").json()
            ids = sorted(provider["id"] for provider in providers["providers"])
            assert ids == sorted(provider.id for provider in registry.all_providers()), "operatori mancanti nel bundle"
            report["provider_ids"] = ids
            resolved = http.get("/api/resolve", params={"q": "MXP"})
            resolved.raise_for_status()
            report["mxp"] = resolved.json()
            assert "Gallarate" in json.dumps(resolved.json())
            tessere = http.get("/api/tessere").json()
            assert "2026-10-02" in json.dumps(tessere)
            report.update(startup=True, no_real_keys=True, catalogue_current=True)
        with sqlite3.connect(installation / "data/trip_finder.db") as db:
            assert db.execute("SELECT value FROM smoke_marker").fetchone() == ("preserve",)
        report["database_preserved"] = True
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in ("provider_ids", "mxp")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
