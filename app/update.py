"""Controllo e installazione degli aggiornamenti del bundle Windows.

Vale solo per chi ha scaricato lo zip dalla release: da sorgente si aggiorna con
`git pull`, e proporre un bottone che sovrascrive una copia di lavoro sarebbe un
ottimo modo per far perdere del lavoro a qualcuno.

La sequenza e' obbligata dal fatto che su Windows un eseguibile in esecuzione e'
bloccato: si scarica lo zip, si scrive un lucchetto, si lancia `Aggiorna.exe` e
si esce. E' quello che sostituisce i file e riapre il programma.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from app.config import DATA_DIR
from app.providers.http_client import HttpError, get_http_client
from app.version import VERSION

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/DiegoRiccardi1234/trip-finder/releases/latest"
ASSET = "TripFinder-windows.zip"

#: Quanto un lucchetto e' da prendere sul serio. Deve superare il caso peggiore
#: dell'aggiornatore — attesa dell'uscita (60 s), respiro (3 s), copia con i
#: suoi tentativi (~31 s) — altrimenti scade a meta' lavoro e ne parte un altro.
DURATA_LUCCHETTO = 180.0

#: Il lanciatore lo registra all'avvio. Serve per uscire in modo ordinato —
#: chiudendo browser Patchright, client HTTP e SQLite — invece di morire e
#: lasciare processi Chromium orfani e il WAL a meta'.
_spegnimento: Callable[[], None] | None = None


def register_shutdown(callback: Callable[[], None]) -> None:
    global _spegnimento
    _spegnimento = callback


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _versione_numerica(testo: str) -> tuple[int, ...]:
    """`v0.10.2` -> (0, 10, 2). Confrontare le stringhe direbbe che 0.9 > 0.10."""
    pulita = testo.strip().lstrip("vV").split("-")[0]
    parti: list[int] = []
    for pezzo in pulita.split("."):
        cifre = "".join(c for c in pezzo if c.isdigit())
        parti.append(int(cifre) if cifre else 0)
    return tuple(parti) or (0,)


def piu_recente(candidata: str, attuale: str = VERSION) -> bool:
    return _versione_numerica(candidata) > _versione_numerica(attuale)


async def check() -> dict[str, Any]:
    """Cosa c'e' pubblicato adesso, senza scaricare niente.

    L'endpoint delle release e' pubblico e non serve autenticazione. Se non
    risponde non e' un errore dell'applicazione: si dice e si va avanti."""
    stato: dict[str, Any] = {
        "installed": VERSION,
        "frozen": is_frozen(),
        "latest": None,
        "update_available": False,
        "notes": "",
        "detail": "",
    }
    try:
        dati = await get_http_client().get_json(
            RELEASES_URL, headers={"Accept": "application/vnd.github+json"}, retries=1
        )
    except HttpError as exc:
        stato["detail"] = f"non sono riuscito a chiedere a GitHub: {exc}"
        return stato

    tag = str((dati or {}).get("tag_name") or "")
    if not tag:
        stato["detail"] = "nessuna release pubblicata"
        return stato

    stato["latest"] = tag
    stato["notes"] = str(dati.get("body") or "")[:4000]
    stato["update_available"] = piu_recente(tag)
    if not is_frozen():
        stato["detail"] = "stai usando il codice sorgente: si aggiorna con git pull"
    elif not any(a.get("name") == ASSET for a in dati.get("assets") or []):
        stato["update_available"] = False
        stato["detail"] = f"la release {tag} non allega {ASSET}"
    return stato


async def install() -> dict[str, Any]:
    """Scarica la versione nuova e passa la mano all'aggiornatore."""
    if not is_frozen():
        return {"started": False, "detail": "l'aggiornamento automatico vale solo per il bundle"}

    try:
        dati = await get_http_client().get_json(
            RELEASES_URL, headers={"Accept": "application/vnd.github+json"}, retries=1
        )
    except HttpError as exc:
        return {"started": False, "detail": f"release non raggiungibile: {exc}"}

    asset = next((a for a in (dati or {}).get("assets") or [] if a.get("name") == ASSET), None)
    if not asset:
        return {"started": False, "detail": f"nessun {ASSET} nella release"}

    cartella = DATA_DIR / "aggiornamenti"
    cartella.mkdir(parents=True, exist_ok=True)
    archivio = cartella / ASSET

    try:
        risposta = await get_http_client().get(str(asset["browser_download_url"]), retries=1)
        archivio.write_bytes(risposta.content)
    except (HttpError, OSError) as exc:
        return {"started": False, "detail": f"scaricamento fallito: {exc}"}

    dest = Path(sys.executable).resolve().parent
    aggiornatore = dest / "Aggiorna.exe"
    if not aggiornatore.exists():
        return {"started": False, "detail": "Aggiorna.exe non c'e': reinstalla il bundle"}

    # Un aggiornamento gia' in corso non se ne fa partire un secondo: due
    # aggiornatori che copiano sugli stessi file si bloccano a vicenda.
    lucchetto = DATA_DIR / "aggiornamento.lock"
    if lucchetto.exists():
        try:
            eta = time.time() - lucchetto.stat().st_mtime
        except OSError:
            eta = DURATA_LUCCHETTO + 1
        if eta < DURATA_LUCCHETTO:
            return {
                "started": False,
                "detail": f"un aggiornamento e' gia' in corso da {int(eta)} secondi",
            }

    # L'aggiornatore va lanciato da **una copia**, non da dove sta installato:
    # fra i file da sostituire c'e' anche lui, e su Windows un eseguibile in
    # esecuzione e' bloccato. La prima versione si riscriveva addosso e moriva
    # con «Il file e' utilizzato da un altro processo».
    try:
        temporaneo = Path(tempfile.mkdtemp(prefix="tripfinder-agg-"))
        copia = temporaneo / aggiornatore.name
        shutil.copy2(aggiornatore, copia)
        # E insieme a lui `_internal`. Il caricatore di PyInstaller cerca li'
        # `python311.dll` **prima** che Python parta: copiando il solo
        # eseguibile muore con «Failed to load Python DLL» e non arriva
        # nemmeno alla prima riga di codice, quindi non lascia traccia
        # nemmeno nel log. Sono un'ottantina di megabyte, e valgono i due
        # secondi che costano.
        interno = aggiornatore.parent / "_internal"
        if interno.is_dir():
            shutil.copytree(interno, temporaneo / "_internal")
    except OSError as exc:
        return {"started": False, "detail": f"non riesco a preparare l'aggiornatore: {exc}"}

    # Il lucchetto dice al programma «non partire, sto lavorando»: senza, un
    # doppio click durante l'aggiornamento riaprirebbe l'eseguibile vecchio e
    # ne bloccherebbe la sostituzione a meta'.
    lucchetto.write_text(str(os.getpid()), encoding="utf-8")

    subprocess.Popen(
        [
            str(copia),
            "--zip", str(archivio),
            "--dest", str(dest),
            "--exe", str(Path(sys.executable).resolve()),
            "--pid", str(os.getpid()),
            "--temporaneo", str(temporaneo),
        ],
        cwd=str(temporaneo),
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    logger.info("aggiornatore avviato, mi spengo")
    # Un istante di respiro perche' la risposta HTTP arrivi al browser prima che
    # il server chiuda: altrimenti la pagina vede una connessione caduta e
    # scrive "errore" proprio mentre tutto sta andando bene.
    asyncio.get_running_loop().call_later(1.5, _esci)
    return {"started": True, "version": dati.get("tag_name")}


def _esci() -> None:
    if _spegnimento is not None:
        _spegnimento()
    else:  # da sorgente non c'e' nessun lanciatore che ci ascolti
        os._exit(0)
