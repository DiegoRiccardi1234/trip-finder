"""Scarica i dataset geografici offline in data/.

Sorgenti:
  - Trainline `stations.csv`: ~50k stazioni europee con gli ID incrociati degli
    operatori (Trenitalia, NTV/Italo, SNCF, DB, Renfe, OBB, FlixBus, Busbud...).
    E' il pezzo che rende possibile parlare con piu' operatori senza mappare
    gli ID a mano.
  - OurAirports `airports.csv`: aeroporti mondiali con IATA e coordinate.

Uso:
    python scripts/fetch_datasets.py [--force]
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

from app.config import DATA_DIR  # noqa: E402

SOURCES: dict[str, list[str]] = {
    # Piu' mirror: il repo Trainline e' stato spostato in passato.
    "stations.csv": [
        "https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv",
        "https://raw.githubusercontent.com/trainline-eu/stations/main/stations.csv",
    ],
    "airports.csv": [
        "https://davidmegginson.github.io/ourairports-data/airports.csv",
        "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv",
    ],
}

MIN_BYTES = 100_000  # sotto questa soglia il download e' quasi certo una pagina d'errore


def download(url: str, dest: Path) -> int:
    from curl_cffi import requests

    with requests.Session(impersonate="chrome") as session:
        response = session.get(url, timeout=120, stream=True)
        response.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        written = 0
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                handle.write(chunk)
                written += len(chunk)
        if written < MIN_BYTES:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"risposta troppo corta ({written} byte)")
        tmp.replace(dest)
        return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="riscarica anche se il file esiste"
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    for filename, urls in SOURCES.items():
        dest = DATA_DIR / filename
        if dest.exists() and not args.force:
            print(f"[skip]  {filename} gia' presente ({dest.stat().st_size:,} byte)")
            continue

        for url in urls:
            try:
                size = download(url, dest)
            except Exception as exc:  # noqa: BLE001 - vogliamo provare il mirror dopo
                print(f"[warn]  {url} -> {exc}")
                continue
            print(f"[ok]    {filename} <- {url} ({size:,} byte)")
            break
        else:
            failures.append(filename)
            print(f"[FAIL]  {filename}: nessun mirror raggiungibile")

    if failures:
        print(f"\nDataset mancanti: {', '.join(failures)}", file=sys.stderr)
        return 1

    print("\nDataset pronti in", DATA_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
