"""Avvia Trip Finder senza finestra, con un'icona nell'area di notifica.

Il server e' un processo lungo, ma la finestra nera del terminale non serve a
niente: non ci si legge nulla di utile e l'unico modo di fermare il programma
era chiuderla. Qui uvicorn gira su un thread e il thread principale viene
ceduto a `pystray`, che su Windows ha bisogno **lui** del ciclo dei messaggi:
se lo si mettesse su un thread secondario l'icona non risponderebbe ai click.

    .venv\\Scripts\\pythonw.exe scripts\\launch_tray.py        senza console
    .venv\\Scripts\\python.exe  scripts\\launch_tray.py --port 9000

Con `pythonw.exe` non esiste nessuna console: `sys.stdout` e `sys.stderr` sono
`None` e la prima riga stampata da una libreria qualunque farebbe morire il
processo senza lasciare traccia. Per questo la prima cosa che accade qui e'
riparare i due flussi, e per questo i log vanno su file: un errore d'avvio
altrimenti sarebbe invisibile.
"""

from __future__ import annotations

import os
import sys


def _harden_stdio() -> None:
    """Sostituisce con /dev/null i flussi che non esistono (caso `pythonw`)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        usable = stream is not None
        if usable:
            try:
                stream.fileno()
            except Exception:
                # StringIO e simili non hanno un descrittore ma sanno scrivere:
                # vanno lasciati stare (succede sotto pytest).
                usable = hasattr(stream, "write")
        if not usable:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


_harden_stdio()

import _bootstrap  # noqa: E402, F401  (radice sul path e uscita in UTF-8)

import argparse  # noqa: E402
import logging  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "data" / "logs" / "trip-finder.log"

#: Porta di partenza, la stessa di `start.ps1`.
DEFAULT_PORT = 8010
#: Quante provare prima di arrendersi, sempre come `start.ps1`.
PORT_SPAN = 20
#: Percorso leggero che risponde solo se e' Trip Finder e non un altro server.
PROBE_PATH = "/api/providers"


def _setup_logging() -> None:
    """Log su file, deciso **prima** di importare l'app.

    `app.main` chiama `logging.basicConfig`, che non fa niente se la radice ha
    gia' un handler: registrando il nostro per primo i log finiscono nel file
    invece che in un flusso che qui non esiste.
    """
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def is_free(port: int) -> bool:
    """Vero se qualcuno puo' ancora mettersi in ascolto su questa porta."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(start: int = DEFAULT_PORT, span: int = PORT_SPAN) -> int:
    """La prima porta libera da `start` in su.

    La porta puo' essere occupata da un altro programma o da un Trip Finder
    rimasto aperto: meglio spostarsi che morire con "address already in use".
    Se sono tutte occupate si torna alla prima e sara' uvicorn a spiegare
    perche' non parte, nel file di log.
    """
    for port in range(start, start + span + 1):
        if is_free(port):
            return port
    return start


def trip_finder_at(port: int, timeout: float = 1.5) -> bool:
    """Vero se su questa porta c'e' gia' un Trip Finder vivo (non un altro server)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{PROBE_PATH}", timeout=timeout) as answer:
            return answer.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def open_when_ready(url: str, port: int, wait: float = 90.0) -> None:
    """Apre il browser quando il server risponde davvero.

    Al primo avvio l'indice geografico ci mette qualche secondo a costruirsi, e
    una pagina aperta troppo presto mostra un errore di connessione che sembra
    un guasto e non lo e'.
    """
    scadenza = time.monotonic() + wait
    while time.monotonic() < scadenza:
        if trip_finder_at(port, timeout=3.0):
            webbrowser.open(url)
            return
        time.sleep(0.7)
    logging.getLogger(__name__).error("il server non ha risposto entro %.0f s", wait)


def copy_to_clipboard(text: str) -> None:
    """Appunti di Windows via `clip.exe`, senza dipendenze e senza finestre."""
    try:
        subprocess.run(
            ["clip"], input=text, text=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        logging.getLogger(__name__).warning("appunti non disponibili", exc_info=True)


def _icon_image():
    """La bussola della favicon, disegnata a runtime: nessun .ico da versionare."""
    from PIL import Image, ImageDraw

    size = 64
    verde = (18, 96, 77, 255)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 3, size - 3), fill=verde)
    draw.ellipse((10, 10, size - 11, size - 11), outline=(255, 255, 255, 255), width=2)
    # L'ago: due triangoli opposti, rosso a nord e bianco a sud.
    draw.polygon([(32, 14), (39, 34), (32, 30), (25, 34)], fill=(235, 96, 84, 255))
    draw.polygon([(32, 50), (25, 30), (32, 34), (39, 30)], fill=(255, 255, 255, 255))
    return image


def run_tray(url: str, server, thread: threading.Thread) -> None:
    """Icona e menu. Se `pystray` manca, il server resta comunque in servizio."""
    log = logging.getLogger(__name__)
    try:
        import pystray
    except Exception:
        # Sotto `pythonw` un'eccezione qui non si vedrebbe: meglio restare vivi
        # e servire il sito, che e' il motivo per cui il programma esiste.
        log.exception("pystray non disponibile: niente icona, il server resta acceso")
        webbrowser.open(url)
        threading.Event().wait()
        return

    def on_open(_icon=None, _item=None) -> None:
        webbrowser.open(url)

    def on_copy(_icon=None, _item=None) -> None:
        copy_to_clipboard(url)

    def on_quit(icon, _item=None) -> None:
        icon.stop()
        # Spegnimento ordinato: il `lifespan` chiude client HTTP, browser
        # Patchright e SQLite. Un `os._exit` immediato lascerebbe processi
        # Chromium orfani e il WAL a meta'.
        server.should_exit = True
        thread.join(timeout=5.0)
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Apri Trip Finder", on_open, default=True),
        pystray.MenuItem("Copia indirizzo", on_copy),
        pystray.MenuItem("Esci", on_quit),
    )
    try:
        icon = pystray.Icon("TripFinder", _icon_image(), f"Trip Finder — {url}", menu)
        icon.run()
    except Exception:
        log.exception("l'icona non e' partita: il server resta acceso")
        threading.Event().wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Trip Finder con icona nell'area di notifica")
    parser.add_argument("--port", type=int, default=None, help=f"porta fissa (default: prima libera da {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="non aprire il browser all'avvio")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger(__name__)

    # Se Trip Finder gira gia', non se ne avvia un secondo: si apre quello.
    # E' anche la guardia contro il doppio click ripetuto sull'icona di avvio.
    # Chi chiede una porta esplicita guarda solo quella.
    da_controllare = ([args.port] if args.port
                      else range(DEFAULT_PORT, DEFAULT_PORT + PORT_SPAN + 1))
    for port in da_controllare:
        if is_free(port):
            continue
        if trip_finder_at(port):
            log.info("Trip Finder e' gia' in ascolto sulla %d: apro il browser", port)
            webbrowser.open(f"http://127.0.0.1:{port}/")
            return 0

    port = args.port if args.port else pick_port()
    url = f"http://127.0.0.1:{port}/"

    import uvicorn
    from app.main import app

    # `log_config=None`: la configurazione dei log l'abbiamo gia' fatta noi, e
    # quella di uvicorn scriverebbe su flussi che qui non esistono.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    log.info("Trip Finder su %s", url)

    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url, port), daemon=True).start()

    run_tray(url, server, thread)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
