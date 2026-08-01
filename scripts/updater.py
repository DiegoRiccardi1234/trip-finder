"""Sostituisce Trip Finder con la versione nuova, e lo riavvia.

Deve essere un programma **a parte**: su Windows un eseguibile in esecuzione e'
bloccato dal sistema e non puo' sovrascriversi da solo. La sequenza e' quindi
sempre la stessa: l'applicazione scarica lo zip, lancia questo, esce; questo
aspetta che il file si sblocchi, sostituisce la cartella e riapre.

    Aggiorna.exe --zip <archivio> --dest <cartella> --exe <TripFinder.exe> --pid <n>

Non ha console e nessuno lo guarda mentre lavora: tutto quello che fa finisce in
`data/logs/aggiornamento.log`, che e' l'unico posto dove si puo' capire perche'
un aggiornamento non e' andato.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

#: Quanto aspettare che il processo vecchio molli i suoi file. Generoso: una
#: chiusura ordinata chiude browser Patchright e SQLite, e con il WAL da
#: scaricare puo' non essere immediata.
ATTESA_USCITA = 60.0

#: Nomi che non vanno mai sovrascritti: sono dell'utente, non del programma.
DA_NON_TOCCARE = {"data", ".env", ".browser-profiles"}


def _log(cartella: Path) -> logging.Logger:
    cartella.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(cartella / "aggiornamento.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("aggiorna")


def _aspetta_uscita(pid: int, log: logging.Logger) -> None:
    """Aspetta che il processo indicato sia davvero morto.

    Copiare mentre e' ancora vivo fallisce con `PermissionError` a meta' strada,
    e a quel punto la cartella e' un misto di due versioni: peggio che non aver
    aggiornato affatto."""
    if not pid:
        time.sleep(2.0)
        return
    scadenza = time.monotonic() + ATTESA_USCITA
    while time.monotonic() < scadenza:
        try:
            os.kill(pid, 0)
        except OSError:
            log.info("il processo %d e' uscito", pid)
            time.sleep(1.0)  # un istante perche' Windows liberi i lock
            return
        time.sleep(0.5)
    log.warning("il processo %d non e' uscito entro %.0fs: provo lo stesso", pid, ATTESA_USCITA)


def _sostituisci(zip_path: Path, dest: Path, log: logging.Logger) -> None:
    """Scompatta sopra l'installazione, senza toccare i dati dell'utente.

    Si scompatta in una cartella accanto e si copia voce per voce, invece di
    svuotare `dest` e riempirla: se lo zip fosse corrotto, cancellare prima
    lascerebbe l'utente senza niente."""
    staging = dest.parent / f"{dest.name}.nuovo"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as archivio:
        archivio.extractall(staging)

    # Lo zip contiene la cartella `TripFinder/`: se c'e', si entra.
    radici = [voce for voce in staging.iterdir() if voce.is_dir()]
    sorgente = radici[0] if len(radici) == 1 and not any(staging.glob("*.exe")) else staging

    for voce in sorgente.iterdir():
        if voce.name in DA_NON_TOCCARE:
            log.info("lascio stare %s: e' roba dell'utente", voce.name)
            continue
        arrivo = dest / voce.name
        if voce.is_dir():
            shutil.copytree(voce, arrivo, dirs_exist_ok=True)
        else:
            shutil.copy2(voce, arrivo)
    shutil.rmtree(staging, ignore_errors=True)
    log.info("sostituzione completata in %s", dest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggiorna Trip Finder")
    parser.add_argument("--zip", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--exe", default="")
    parser.add_argument("--pid", type=int, default=0)
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    log = _log(dest / "data" / "logs")
    log.info("aggiornamento da %s verso %s", args.zip, dest)

    try:
        _aspetta_uscita(args.pid, log)
        _sostituisci(Path(args.zip).resolve(), dest, log)
    except Exception:
        log.exception("aggiornamento fallito: resta installata la versione di prima")
        return 1
    finally:
        # Il segnale che dice all'applicazione «non partire, sto lavorando».
        (dest / "data" / "aggiornamento.lock").unlink(missing_ok=True)

    exe = Path(args.exe) if args.exe else dest / "TripFinder.exe"
    if exe.exists():
        log.info("riavvio %s", exe)
        ambiente = dict(os.environ, TRIPFINDER_AGGIORNATO="1")
        subprocess.Popen([str(exe)], cwd=str(dest), env=ambiente, close_fds=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
