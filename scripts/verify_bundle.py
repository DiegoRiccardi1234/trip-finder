"""Verifica ZIP, aggiornamento e avvio del bundle in un'installazione isolata.

Non tocca l'app installata. Il workspace temporaneo e il report restano
disponibili per ispezione; nessuna chiave reale viene copiata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from contextlib import closing

import _bootstrap  # noqa: F401
import httpx

from app.update_sync import sincronizza
from app.providers import registry
from app.version import VERSION
from app.install_layout import is_organized, state_dir
from scripts.updater import _vivo


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
    with args.zip.open("rb") as package:
        digest = hashlib.file_digest(package, "sha256").hexdigest()
    report = {"version": VERSION, "workspace": str(workspace), "archive": str(args.zip.resolve()), "archive_sha256": digest}
    with zipfile.ZipFile(args.zip) as archive:
        names = archive.namelist()
        forbidden = {".env", ".env.local", "local_secrets.json", "trip_finder.db"}
        assert not any(forbidden.intersection(Path(name).parts) or "/logs/" in name for name in names), "dati personali nello ZIP"
        organized = "TripFinder/app/.tripfinder-layout.json" in names
        helper = "TripFinder/app/TripFinderMCP.exe" if organized else "TripFinder/TripFinderMCP.exe"
        assert helper in names, "helper MCP console mancante"
        dataset_prefix = "app/resources/data" if organized else "data"
        assert all(f"TripFinder/{dataset_prefix}/{name}" in names for name in ("stations.csv", "airports.csv", "cities15000.txt"))
        assert archive.testzip() is None, "ZIP corrotto"
        archive.extractall(extraction)
    if organized:
        import shutil
        shutil.copytree(extraction / "TripFinder", installation)
    data = state_dir(installation)
    data.mkdir(parents=True, exist_ok=True)
    relative_data = data.relative_to(installation).as_posix()
    env_name = relative_data + "/.env" if organized else ".env"
    markers = {env_name: "# installazione di prova\nALLOW_PAID_PROVIDERS=false\n", relative_data + "/local_secrets.json": "{}\n", relative_data + "/profile-marker.txt": "preserve\n"}
    for name, content in markers.items():
        (installation / name).write_text(content, encoding="utf-8")
    with closing(sqlite3.connect(data / "trip_finder.db")) as db:
        db.execute("CREATE TABLE smoke_marker(value TEXT)")
        db.execute("INSERT INTO smoke_marker VALUES ('preserve')")
        db.commit()
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
    runtime_pid = None
    try:
        deadline = time.monotonic() + 90
        with httpx.Client(base_url="http://127.0.0.1:8099", timeout=10, trust_env=False) as http:
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
            descriptor = json.loads((data / "runtime.json").read_text(encoding="utf-8"))
            identity = http.get("/api/runtime").json()
            assert identity["instance_id"] == descriptor["instance_id"]
            assert descriptor["base_url"].rstrip("/") == "http://127.0.0.1:8099"
            runtime_pid = int(descriptor["pid"])
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
            expected = json.loads((Path(__file__).resolve().parents[1] / "app" / "routing" / "data" / "tessere.json").read_text(encoding="utf-8"))
            assert tessere["generato_il"] == expected["generato_il"]
            actual_cards = {card["id"]: card for card in tessere["tessere"]}
            assert set(actual_cards) == {card["id"] for card in expected["tessere"]}
            assert all(all(actual_cards[card["id"]].get(key) == value for key, value in card.items()) for card in expected["tessere"])
            report.update(tessere_generated=tessere["generato_il"], tessere_count=len(actual_cards), tessere_all_source_fields_match=True)
            report["young_senior"] = {key: {field: card[field] for field in ("valido_da", "valido_a", "valore", "verificato_il") if field in card}
                                      for key, card in actual_cards.items() if "promo" in key and ("young" in key or "senior" in key)}
            report.update(startup=True, no_real_keys=True, catalogue_current=True)
    finally:
        if os.name == "nt" and process.poll() is None:
            # The organized entry point owns a runtime child: terminate only
            # this verifier's known process tree, never a pre-existing app.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if runtime_pid and _vivo(runtime_pid):
            subprocess.run(["taskkill", "/PID", str(runtime_pid), "/T", "/F"], capture_output=True, check=False)
            deadline = time.monotonic() + 15
            while _vivo(runtime_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not _vivo(runtime_pid), "Il runtime sintetico deve fermarsi prima della verifica SQLite"
        with closing(sqlite3.connect(data / "trip_finder.db")) as db:
            assert db.execute("SELECT value FROM smoke_marker").fetchone() == ("preserve",)
        report["database_preserved"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in ("provider_ids", "mxp")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
