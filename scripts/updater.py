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

from app.update_sync import sincronizza  # noqa: E402

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
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    kernel = ctypes.windll.kernel32
    # `PROCESS_QUERY_LIMITED_INFORMATION` e non `SYNCHRONIZE`: con il secondo
    # l'handle si apre ma `GetExitCodeProcess` non ha i diritti per rispondere,
    # fallisce, e l'attesa dichiara uscito un processo ancora vivo. Nel log si
    # vede come «il processo N e' uscito» nello stesso secondo in cui e' partito.
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False  # non esiste piu', o non e' nostro: in entrambi i casi via
    try:
        ANCORA_ATTIVO = 259
        codice = ctypes.c_ulong()
        if kernel.GetExitCodeProcess(handle, ctypes.byref(codice)):
            return codice.value == ANCORA_ATTIVO
        return False
    finally:
        kernel.CloseHandle(handle)


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
    scrivi(f"il processo {pid} non e' uscito entro {ATTESA_USCITA:.0f}s: provo lo stesso")


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
    parser.add_argument(
        "--temporaneo", default="",
        help="la cartella da cui sto girando, da cancellare quando ho finito",
    )
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    file_log = _log(dest / "data" / "logs")

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
                    archivio.extractall(scompattato)
                scrivi(f"archivio scompattato in {scompattato}")

                scritti = sincronizza(origine=_radice(scompattato), destinazione=dest)
                scrivi(f"copiati {scritti} file, data/ intatta")
        except Exception as exc:
            scrivi(f"FALLITO: {exc!r}")
            scrivi("resta installata la versione di prima")
            with contextlib.suppress(OSError):
                (dest / "data" / "aggiornamento.lock").unlink(missing_ok=True)
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
                    [str(exe)],
                    cwd=str(dest),
                    close_fds=True,
                    creationflags=bandiere,
                    env={**os.environ, "TRIPFINDER_AGGIORNATO": "1"},
                )
        else:
            scrivi(f"non trovo {exe}: non riapro niente")

        with contextlib.suppress(OSError):
            (dest / "data" / "aggiornamento.lock").unlink(missing_ok=True)

        # La cartella da cui stiamo girando non si puo' cancellare da vivi: la
        # affidiamo a un comando staccato che aspetta e poi la toglie.
        if args.temporaneo and sys.platform == "win32":
            with contextlib.suppress(OSError):
                subprocess.Popen(
                    ["cmd", "/c", f'timeout /t 6 /nobreak >nul & rmdir /s /q "{args.temporaneo}"'],
                    close_fds=True,
                    creationflags=subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
