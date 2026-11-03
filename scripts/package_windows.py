"""Produce both the historical updater transport and the organized portable ZIP."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from app.install_layout import DATASETS, LAYOUT_MARKER, LAYOUT_VERSION, is_organized

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {"state", ".env", ".env.local", ".browser-profiles"}


def organize_bundle(source: Path, target: Path) -> Path:
    source, target = source.resolve(), target.resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Le cartelle sorgente e destinazione devono essere separate")
    if not all((source / name).is_file() for name in ("TripFinder.exe", "TripFinderMCP.exe")) or not (source / "_internal").is_dir():
        raise ValueError("Runtime flat incompleto")
    if target.exists() and any(target.iterdir()):
        raise ValueError("La destinazione deve essere vuota")
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise RuntimeError("Compilatore C# .NET Framework mancante")
    runtime = target / "app"
    runtime.mkdir(parents=True)
    (target / "state").mkdir()
    for entry in source.iterdir():
        if entry.name in PRIVATE or entry.name in {"data", "LEGGIMI.txt"}:
            continue
        destination = runtime / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination)
        else:
            shutil.copy2(entry, destination)
    public_data = runtime / "resources" / "data"
    public_data.mkdir(parents=True)
    for name in DATASETS:
        shutil.copy2(source / "data" / name, public_data / name)
    (runtime / LAYOUT_MARKER).write_text(json.dumps({"version": LAYOUT_VERSION}) + "\n", encoding="utf-8")
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                    "/win32icon:" + str(ROOT / "app/static/tripfinder.ico"),
                    "/out:" + str(target / "TripFinder.exe"), str(ROOT / "scripts/windows_launcher.cs")],
                   check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    shutil.copy2(source / "LEGGIMI.txt", target / "LEGGIMI.txt")
    return target


def write_archive(source: Path, destination: Path, *, organized: bool = False) -> Path:
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("L'archivio deve stare fuori dal pacchetto")
    if organized and not is_organized(source):
        raise ValueError("Marker del pacchetto organizzato non valido")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        if organized:
            archive.writestr("TripFinder/state/", "")
        for entry in source.rglob("*"):
            relative = entry.relative_to(source)
            if relative.parts[0] in PRIVATE or (relative.parts[0] == "data" and (len(relative.parts) != 2 or relative.name not in DATASETS)):
                continue
            if entry.is_file():
                if entry.is_symlink() or not entry.resolve().is_relative_to(source.resolve()):
                    raise ValueError("Collegamento non ammesso nel pacchetto")
                archive.write(entry, Path("TripFinder") / relative)
    return destination
