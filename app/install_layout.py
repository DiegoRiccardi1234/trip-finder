"""Installation paths, without importing configuration or creating state."""

from __future__ import annotations

import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

LAYOUT_MARKER = ".tripfinder-layout.json"
LAYOUT_VERSION = 1
DATASETS = ("stations.csv", "airports.csv", "cities15000.txt")


def is_organized(root: Path) -> bool:
    try:
        marker = json.loads((Path(root) / "app" / LAYOUT_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and type(marker.get("version")) is int and marker["version"] == LAYOUT_VERSION


def validate_layout(root: Path) -> None:
    if (Path(root) / "app" / LAYOUT_MARKER).exists() and not is_organized(root):
        raise ValueError("Layout Trip Finder non riconosciuto: ripristina il marker prima di avviare")


def installation_root(executable_or_dir: Path | None = None) -> Path:
    target = Path(executable_or_dir or sys.executable).resolve()
    directory = target.parent if target.suffix.lower() == ".exe" or target.is_file() else target
    if directory.name.lower() == "app":
        validate_layout(directory.parent)
        if is_organized(directory.parent):
            return directory.parent
    validate_layout(directory)
    return directory


def runtime_dir(root: Path) -> Path:
    validate_layout(root)
    return Path(root) / "app" if is_organized(root) else Path(root)


def state_dir(root: Path) -> Path:
    validate_layout(root)
    return Path(root) / "state" if is_organized(root) else Path(root) / "data"


def env_file(root: Path) -> Path:
    return state_dir(root) / ".env" if is_organized(root) else Path(root) / ".env"


def seed_datasets(root: Path) -> None:
    """Only fill missing public catalogues; never overwrite user state."""
    if not is_organized(root):
        return
    destination = state_dir(root)
    destination.mkdir(parents=True, exist_ok=True)
    for name in DATASETS:
        source = runtime_dir(root) / "resources" / "data" / name
        target = destination / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)


@contextmanager
def state_ownership(data_directory: Path):
    """Hold one OS lock for the lifetime of the sole application/state owner."""
    data_directory.mkdir(parents=True, exist_ok=True)
    handle = (data_directory / "instance.lock").open("a+b")
    try:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Questa installazione Trip Finder è già aperta: usa il processo esistente") from exc
        yield
    finally:
        handle.close()
