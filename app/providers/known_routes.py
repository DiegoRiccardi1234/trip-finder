"""Operatori che coprono una tratta ma che non sappiamo interrogare.

Il time-box della scoperta lascia sempre qualcuno fuori: Italo vuole un token
che nasce nella sua pagina, Tirrenia pubblica le partenze senza orario di
arrivo ne' prezzo. Scrivere un adapter che finge di funzionare sarebbe la cosa
peggiore, ma tacere non e' molto meglio: chi cerca Torino-Roma e non vede Italo
puo' concludere che non ci sia, e sbagliare viaggio.

Qui sta la terza via. L'elenco e' in `known_routes.json`, con l'evidenza in
`docs/operatori.md`, e la ricerca lo usa per dire: *"anche questi collegano le
due citta', ma i loro orari vanno visti sul loro sito"*, con il link.

Nessuna copertura e' dichiarata a occhio: o l'operatore compare nei dataset con
i propri identificatori di stazione, o le sue rotte sono state lette dai suoi
documenti pubblici.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.models import Mode, Place

logger = logging.getLogger(__name__)

CATALOG = Path(__file__).with_name("known_routes.json")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return " ".join(cleaned.split())


@lru_cache(maxsize=1)
def _catalog() -> list[dict[str, Any]]:
    if not CATALOG.exists():
        return []
    try:
        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    except ValueError:
        logger.exception("known_routes.json non leggibile: lo ignoro")
        return []
    return [entry for entry in payload.get("operators", []) if entry.get("id")]


def _place_names(place: Place) -> set[str]:
    """Come si puo' chiamare questo posto: l'etichetta e le citta' dei suoi nodi."""
    names = {_normalize(place.label), _normalize(place.query)}
    for node in place.nodes:
        if node.city:
            names.add(_normalize(node.city))
        names.add(_normalize(node.name.split("(")[0]))
    return {name for name in names if name}


def _covers_by_dataset(entry: dict, origin: Place, destination: Place) -> bool:
    key = entry.get("dataset_key")
    if not key:
        return False
    return any(node.supports(key) for node in origin.nodes) and any(
        node.supports(key) for node in destination.nodes
    )


def _covers_by_routes(entry: dict, origin_names: set[str], dest_names: set[str]) -> bool:
    for route in entry.get("routes") or []:
        if len(route) != 2:
            continue
        first, second = _normalize(route[0]), _normalize(route[1])
        # Le rotte valgono nei due versi: chi fa Napoli-Palermo fa anche il
        # ritorno, e nessun documento le elenca due volte.
        if (first in origin_names and second in dest_names) or (
            second in origin_names and first in dest_names
        ):
            return True
    return False


def suggestions(
    origin: Place, destination: Place, modes: set[Mode] | None = None
) -> list[dict[str, str]]:
    """Operatori non interrogabili che collegano davvero queste due localita'."""
    origin_names = _place_names(origin)
    dest_names = _place_names(destination)

    found: list[dict[str, str]] = []
    for entry in _catalog():
        mode = entry.get("mode")
        if modes is not None and mode and Mode(mode) not in modes:
            continue
        if _covers_by_dataset(entry, origin, destination) or _covers_by_routes(
            entry, origin_names, dest_names
        ):
            found.append(
                {
                    "id": entry["id"],
                    "name": entry.get("name") or entry["id"],
                    "mode": mode or "",
                    "url": entry.get("url") or "",
                    "note": entry.get("note") or "",
                }
            )
    return found
