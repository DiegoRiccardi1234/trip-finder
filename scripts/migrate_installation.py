"""Guided migration. Close Trip Finder first; originals remain in --backup."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.install_migration import migrate_installation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Cartella estratta da TripFinder-portable.zip")
    parser.add_argument("--target", type=Path, required=True, help="Installazione flat chiusa")
    parser.add_argument("--backup", type=Path, required=True, help="Nuova cartella backup esterna")
    args = parser.parse_args()
    print(migrate_installation(source=args.source, target=args.target, backup=args.backup))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
