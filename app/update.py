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
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from app.config import DATA_DIR
from app.providers.http_client import HttpError, get_http_client
from app.version import VERSION

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/DiegoRiccardi1234/trip-finder/releases/latest"
ASSET = "TripFinder-windows.zip"

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

    # Il lucchetto dice al programma «non partire, sto lavorando»: senza, un
    # doppio click durante l'aggiornamento riaprirebbe l'eseguibile vecchio e
    # ne bloccherebbe la sostituzione a meta'.
    (DATA_DIR / "aggiornamento.lock").write_text(str(os.getpid()), encoding="utf-8")

    subprocess.Popen(
        [
            str(aggiornatore),
            "--zip", str(archivio),
            "--dest", str(dest),
            "--exe", str(Path(sys.executable).resolve()),
            "--pid", str(os.getpid()),
        ],
        cwd=str(dest),
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
