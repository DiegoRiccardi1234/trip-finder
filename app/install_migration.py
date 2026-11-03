"""Explicit, reversible migration of a closed legacy installation."""
from __future__ import annotations

import os
import shutil
import tempfile
import sqlite3
from contextlib import closing
from pathlib import Path

from app.install_layout import is_organized, state_dir, state_ownership, validate_layout
from app.update_sync import claim_update_lock, release_update_lock


def _without_reparse_points(root: Path) -> None:
    for entry in root.rglob("*"):
        if entry.is_symlink() or getattr(entry.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("La migrazione non segue symlink o junction: conserva gli originali e verifica il percorso")


def migrate_installation(*, source: Path, target: Path, backup: Path) -> Path:
    source, target, backup = (path.resolve() for path in (source, target, backup))
    if not is_organized(source) or not (source / "TripFinder.exe").is_file():
        raise ValueError("Serve un pacchetto portable organizzato valido")
    validate_layout(target)
    if is_organized(target):
        raise ValueError("L'installazione è già organizzata")
    if not (target / "TripFinder.exe").is_file():
        raise ValueError("Installazione legacy non trovata")
    if backup.exists() or backup.is_relative_to(target) or target.is_relative_to(backup):
        raise ValueError("Il backup deve essere una destinazione nuova esterna all'installazione")
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Il pacchetto sorgente deve essere separato dall'installazione")
    _without_reparse_points(source)
    _without_reparse_points(target)
    if os.name == "nt":
        # Windows refuses write access to a running executable image. This
        # also detects legacy releases which predate our state-owner lock.
        try:
            with (target / "TripFinder.exe").open("r+b"):
                pass
        except OSError as exc:
            raise RuntimeError("Chiudi Trip Finder prima della migrazione") from exc
    data = state_dir(target)
    owner = claim_update_lock(data / "aggiornamento.lock", "layout-migration")
    try:
        with tempfile.TemporaryDirectory(prefix="tripfinder-migration-", dir=target.parent) as temporary:
            staged = Path(temporary) / target.name
            with state_ownership(data):
                # Copying SQLite is consistent here: the sole owner lock is
                # held, so no running application may write the store.
                # The portable used to launch this command may itself have
                # been opened before. Its personal state must never enter the
                # installation being migrated.
                shutil.copytree(source, staged, ignore=lambda directory, names: set(names) & {"state", ".env", ".env.local", ".browser-profiles"} if Path(directory).resolve() == source else set())
                (staged / "state").mkdir(exist_ok=True)
                shutil.copytree(data, staged / "state", dirs_exist_ok=True, ignore=shutil.ignore_patterns("instance.lock", "aggiornamento.lock", "runtime.json"))
                database = data / "trip_finder.db"
                if database.is_file():
                    snapshot = staged / "state" / ".migration-snapshot.db"
                    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original:
                        with closing(sqlite3.connect(snapshot)) as copied:
                            original.backup(copied)
                            if copied.execute("PRAGMA quick_check").fetchone() != ("ok",):
                                raise RuntimeError("Snapshot SQLite non valido")
                            copied.commit()
                    os.replace(snapshot, staged / "state" / database.name)
                    for suffix in ("-wal", "-shm"):
                        (staged / "state" / (database.name + suffix)).unlink(missing_ok=True)
                (staged / "state" / "aggiornamento.lock").unlink(missing_ok=True)
                (staged / "state" / "runtime.json").unlink(missing_ok=True)
                for name in (".env", ".env.local", ".browser-profiles"):
                    old = target / name
                    if old.is_dir():
                        shutil.copytree(old, staged / "state" / name, dirs_exist_ok=True)
                    elif old.is_file():
                        shutil.copy2(old, staged / "state" / name)
                if (target / "vendor").is_dir():
                    shutil.copytree(target / "vendor", staged / "app" / "vendor", dirs_exist_ok=True)
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
            try:
                os.replace(staged, target)
            except Exception:
                os.replace(backup, target)
                raise
            # Preserve the original tree as the rollback checkpoint. It is
            # never recursively removed by this operation.
            release_update_lock(backup / "data" / "aggiornamento.lock", owner)
    except Exception:
        release_update_lock(data / "aggiornamento.lock", owner)
        raise
    return backup


def restore_installation(*, target: Path, backup: Path, failed: Path) -> None:
    target, backup, failed = (path.resolve() for path in (target, backup, failed))
    if not backup.is_dir() or failed.exists() or failed.is_relative_to(target) or backup.is_relative_to(target):
        raise ValueError("Percorsi rollback non validi")
    with state_ownership(state_dir(target)):
        pass
    os.replace(target, failed)
    try:
        os.replace(backup, target)
    except Exception:
        os.replace(failed, target)
        raise
