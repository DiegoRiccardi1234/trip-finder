"""Sostituisce Trip Finder con la versione nuova, e lo riapre.

Deve essere un programma **a parte**: su Windows un eseguibile in esecuzione e'
bloccato dal sistema e non puo' sovrascriversi da solo. La sequenza e' sempre la
stessa: l'applicazione scarica lo zip, copia questo aggiornatore nel
temporaneo, lo lancia da li' ed esce; questo aspetta che i file si sblocchino,
sostituisce la cartella e riapre.

    Aggiorna.exe --zip <archivio> --dest <cartella> --pid <n> [--temporaneo <dir>]

Nessuno lo guarda mentre lavora — non ha finestra — quindi tutto finisce in
`data/logs/aggiornamento.log`, l'unico posto dove si puo' capire perche' un
aggiornamento non e' andato.

Le quattro regole qui sotto vengono da un fallimento vero, il 2026-08-01, e
ciascuna e' costata lo stesso errore: `PermissionError [WinError 32] Il file e'
utilizzato da un altro processo`.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def _prepara_import() -> None:
    """Rende importabile `app.*` anche girando dal temporaneo.

    PyInstaller mette le dipendenze in `_internal`, accanto all'eseguibile.
    Questo pero' gira da una copia in `%TEMP%` — apposta, per non riscrivere sé
    stesso durante la copia — e li' `_internal` non c'e': va cercato nella
    cartella di installazione, che arriva come argomento."""
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        return
    candidati = [Path(sys.executable).resolve().parent / "_internal"]
    for indice, argomento in enumerate(sys.argv):
        if argomento == "--dest" and indice + 1 < len(sys.argv):
            candidati.append(Path(sys.argv[indice + 1]).resolve() / "_internal")
    for candidato in candidati:
        if candidato.exists():
            sys.path.insert(0, str(candidato))
            return


_prepara_import()

from app.update_sync import sincronizza, release_update_lock, _pid_alive  # noqa: E402
from app.install_layout import state_dir  # noqa: E402

#: Quanto aspettare che il processo vecchio molli i suoi file.
ATTESA_USCITA = 60.0

#: E quanto aspettare **dopo** che e' uscito. Windows impiega qualche secondo a
#: rilasciare gli handle ereditati — il browser di Patchright, il WAL di SQLite,
#: la scansione dell'antivirus sull'archivio appena scompattato. Copiare subito
#: si scontra con quelli, e il log del 2026-08-01 dice esattamente questo: «il
#: processo e' uscito» alle 23:25:53, PermissionError alle 23:25:56.
RESPIRO = 3.0


def _log(cartella: Path):
    cartella.mkdir(parents=True, exist_ok=True)
    return (cartella / "aggiornamento.log").open("a", encoding="utf-8")


def _vivo(pid: int) -> bool:
    """Vero se quel processo sta ancora girando.

    **Non** con `os.kill(pid, 0)`: su Windows Python lo traduce in
    `TerminateProcess`, quindi non e' una domanda, e' un'esecuzione — e
    comunque tornava subito, facendo credere che il vecchio fosse uscito quando
    stava ancora chiudendo. Qui si chiede al sistema, e basta."""
    return _pid_alive(pid)


def _aspetta(pid: int, scrivi) -> None:
    if not pid:
        time.sleep(RESPIRO)
        return
    scadenza = time.monotonic() + ATTESA_USCITA
    while time.monotonic() < scadenza:
        if not _vivo(pid):
            scrivi(f"il processo {pid} e' uscito")
            return
        time.sleep(0.3)
    raise TimeoutError(f"il processo {pid} non e' uscito entro {ATTESA_USCITA:.0f}s: aggiornamento annullato")


def _radice(scompattato: Path) -> Path:
    """Lo zip contiene la cartella `TripFinder/`: se c'e', si entra."""
    cartelle = [voce for voce in scompattato.iterdir() if voce.is_dir()]
    if len(cartelle) == 1 and not any(scompattato.glob("*.exe")):
        return cartelle[0]
    return scompattato


def _avviso_se_lanciato_a_mano() -> None:
    """Chi ci fa doppio click merita una spiegazione, non un errore muto."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        ctypes.windll.user32.MessageBoxW(
            0,
            "Aggiorna.exe lo avvia Trip Finder da solo.\n\n"
            "Apri TripFinder.exe e usa «Controlla aggiornamenti» "
            "nella scheda Impostazioni.",
            "Aggiornamento di Trip Finder",
            0x00000040,
        )


def main() -> int:
    if len(sys.argv) <= 1:
        _avviso_se_lanciato_a_mano()
        return 0

    parser = argparse.ArgumentParser(description="Aggiorna Trip Finder")
    parser.add_argument("--zip", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--exe", default="")
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--lock-owner", default="")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument(
        "--temporaneo", default="",
        help="la cartella da cui sto girando, da cancellare quando ho finito",
    )
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    if not 1 <= args.port <= 65535:
        return 2
    data = state_dir(dest)
    file_log = _log(data / "logs")
    lock = data / "aggiornamento.lock"

    def scrivi(messaggio: str) -> None:
        file_log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {messaggio}\n")
        file_log.flush()

    with contextlib.closing(file_log):
        try:
            scrivi(f"aggiornamento da {args.zip} verso {dest}")
            _aspetta(args.pid, scrivi)
            time.sleep(RESPIRO)

            with tempfile.TemporaryDirectory(prefix="tripfinder-agg-") as temporaneo:
                scompattato = Path(temporaneo) / "nuovo"
                scompattato.mkdir()
                with zipfile.ZipFile(Path(args.zip).resolve()) as archivio:
                    for member in archivio.infolist():
                        relative = Path(member.filename.replace("\\", "/"))
                        if relative.is_absolute() or ".." in relative.parts or any(":" in part for part in relative.parts):
                            raise ValueError("Percorso non valido nell'archivio")
                        if (member.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError("Collegamento simbolico nell'archivio")
                    archivio.extractall(scompattato)
                scrivi(f"archivio scompattato in {scompattato}")

                scritti = sincronizza(origine=_radice(scompattato), destinazione=dest)
                scrivi(f"copiati {scritti} file, data/ intatta")
        except Exception as exc:
            scrivi(f"FALLITO: {exc!r}")
            scrivi("copia interrotta: i file già sostituiti sono stati ripristinati")
            with contextlib.suppress(OSError):
                release_update_lock(lock, args.lock_owner or None, legacy_parent_pid=args.pid or None)
            return 1

        exe = Path(args.exe) if args.exe else dest / "TripFinder.exe"
        if exe.exists():
            scrivi(f"riavvio {exe}")
            # `CREATE_NO_WINDOW` e non `DETACHED_PROCESS`: staccato del tutto, il
            # processo nuovo eredita handle di standard output non validi e muore
            # alla prima riga che scrive, prima ancora di aprire la porta. Da
            # fuori sembra che l'aggiornamento non finisca mai — ed e' esattamente
            # quello che si vedeva: «Il programma non e' tornato su».
            bandiere = 0
            if sys.platform == "win32":
                bandiere = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            with contextlib.suppress(OSError):
                subprocess.Popen(
                    [str(exe), "--port", str(args.port)],
                    cwd=str(dest),
                    close_fds=True,
                    creationflags=bandiere,
                    env={**os.environ, "TRIPFINDER_AGGIORNATO": "1"},
                )
        else:
            scrivi(f"non trovo {exe}: non riapro niente")

        with contextlib.suppress(OSError):
            release_update_lock(lock, args.lock_owner or None, legacy_parent_pid=args.pid or None)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
