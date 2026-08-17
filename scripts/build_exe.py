"""Costruisce il bundle Windows autonomo.

    python scripts/build_exe.py

Produce:
    dist/TripFinder/                 la cartella da scompattare, con TripFinder.exe
    dist/TripFinder/data/*.csv       i cataloghi delle fermate, dentro lo zip
    dist/TripFinder-windows.zip      quello che si allega alla release

I due cataloghi (stazioni e aeroporti, ~30 MB in chiaro, ~12 compressi) entrano
nello zip di proposito. Scaricarli al primo avvio avrebbe tenuto l'archivio piu'
leggero, ma avrebbe anche voluto dire che il primo doppio click chiede la rete,
mostra una barra e puo' fallire — per dei dati che cambiano una volta ogni mai.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "TripFinder.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
BUNDLE = DIST / "TripFinder"

#: I cataloghi che il resolver geografico non puo' non avere.
#: Quello che finisce nello zip. Sviluppo e bundle sono due ambienti diversi:
#: `data/` qui e' pieno, nel bundle c'e' **solo** cio' che questa tupla nomina.
#: Una funzione che dipende da un dato nuovo va aggiunta qui, o funziona per chi
#: sviluppa e non per chi scarica — che e' esattamente com'e' uscita la 0.5.0:
#: il gazetteer mondiale c'era nel repo e non nello zip, quindi nell'app
#: scaricabile «Tokyo» non esisteva e «Londra» risolveva su Ondara, in Spagna.
DATASETS = ("stations.csv", "airports.csv", "cities15000.txt")


def _assicura_datasets() -> bool:
    """Scarica i cataloghi se mancano: in CI la cartella `data/` non esiste."""
    mancanti = [nome for nome in DATASETS if not (ROOT / "data" / nome).is_file()]
    if not mancanti:
        return True
    print(f"cataloghi mancanti ({', '.join(mancanti)}): li scarico")
    esito = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "fetch_datasets.py")], cwd=ROOT, check=False
    )
    if esito.returncode != 0:
        print("ATTENZIONE: download fallito, il bundle partira' senza fermate", file=sys.stderr)
        return False
    return all((ROOT / "data" / nome).is_file() for nome in DATASETS)


def _porta_i_datasets() -> None:
    destinazione = BUNDLE / "data"
    destinazione.mkdir(parents=True, exist_ok=True)
    for nome in DATASETS:
        sorgente = ROOT / "data" / nome
        if sorgente.is_file():
            shutil.copy2(sorgente, destinazione / nome)
            print(f"  {nome}: {sorgente.stat().st_size // (1024 * 1024)} MB")


def main() -> int:
    if not SPEC.exists():
        print(f"manca lo spec: {SPEC}", file=sys.stderr)
        return 1

    _assicura_datasets()

    for cartella in (DIST, BUILD):
        if cartella.exists():
            shutil.rmtree(cartella)

    print("PyInstaller in corso...")
    # `-m PyInstaller` e non il comando nudo: `pyinstaller.exe` sta in
    # `.venv/Scripts/`, che e' nel PATH solo con l'ambiente **attivato**. Chi
    # lancia questo script col python del venv per percorso — come fa chiunque
    # automatizzi qualcosa — riceveva `WinError 2: impossibile trovare il file
    # specificato`, che non nomina PyInstaller e sembra un problema dello spec.
    # Due righe piu' su `_assicura_datasets` gia' usava `sys.executable`.
    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"], cwd=ROOT
    )
    if not BUNDLE.exists():
        print("PyInstaller non ha prodotto dist/TripFinder/", file=sys.stderr)
        return 2

    print("cataloghi delle fermate:")
    _porta_i_datasets()

    guida = ROOT / "scripts" / "bundle_LEGGIMI.txt"
    if guida.exists():
        shutil.copy2(guida, BUNDLE / "LEGGIMI.txt")

    zip_path = DIST / "TripFinder-windows.zip"
    print(f"creo {zip_path.name}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archivio:
        for file in BUNDLE.rglob("*"):
            if file.is_file():
                archivio.write(file, file.relative_to(BUNDLE.parent))
    print(f"fatto: {zip_path} ({zip_path.stat().st_size // (1024 * 1024)} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
