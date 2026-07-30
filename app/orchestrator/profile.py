"""Profilo: ricerche salvate e preferenze, sul database del progetto.

Una ricerca salvata **e' la sua query string**, la stessa che sta
nell'indirizzo della pagina. Cosi' salvare e condividere sono la stessa cosa, e
riaprire una ricerca non richiede di ricostruire niente: si rimettono i
parametri nell'indirizzo e il frontend fa il resto.

Sta su SQLite e non in `localStorage` perche' pulire i dati di navigazione, o
aprire l'app da un altro browser, non deve far sparire quello che l'utente ha
messo da parte.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import orjson

from app.models import Discount
from app.orchestrator.db import get_db

logger = logging.getLogger(__name__)

#: Oltre questa soglia le ricerche piu' vecchie vengono dimenticate: e' un
#: elenco da consultare a colpo d'occhio, non un archivio.
MAX_SAVED = 100

#: Le sole preferenze riconosciute. Una chiave non prevista viene ignorata
#: invece di finire nel database e restarci per sempre.
KNOWN_PREFERENCES = frozenset({"home", "preset", "modes", "pax", "sort"})


async def list_searches() -> list[dict[str, Any]]:
    db = await get_db()
    async with db.execute(
        "SELECT id, name, params, summary, created_at "
        "FROM saved_searches ORDER BY created_at DESC"
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "params": row[2],
            "summary": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


async def save_search(name: str, params: str, summary: str | None = None) -> dict[str, Any]:
    """Salva, o aggiorna se un nome uguale c'e' gia'.

    Rifare la stessa ricerca e risalvarla e' il gesto piu' probabile: senza
    l'aggiornamento l'elenco si riempirebbe di doppioni con lo stesso nome."""
    db = await get_db()
    now = time.time()
    async with db.execute(
        "SELECT id FROM saved_searches WHERE name = ?", (name,)
    ) as cursor:
        existing = await cursor.fetchone()

    if existing:
        await db.execute(
            "UPDATE saved_searches SET params = ?, summary = ?, created_at = ? WHERE id = ?",
            (params, summary, now, existing[0]),
        )
        saved_id = existing[0]
    else:
        cursor = await db.execute(
            "INSERT INTO saved_searches (name, params, summary, created_at) VALUES (?, ?, ?, ?)",
            (name, params, summary, now),
        )
        saved_id = cursor.lastrowid
        await db.execute(
            "DELETE FROM saved_searches WHERE id NOT IN "
            "(SELECT id FROM saved_searches ORDER BY created_at DESC LIMIT ?)",
            (MAX_SAVED,),
        )
    await db.commit()
    return {"id": saved_id, "name": name, "params": params, "summary": summary, "created_at": now}


async def delete_search(saved_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM saved_searches WHERE id = ?", (saved_id,))
    await db.commit()
    return bool(cursor.rowcount)


async def list_discounts() -> list[Discount]:
    """Le tessere dichiarate, quelle spente comprese.

    Il record e' un `Discount` serializzato invece di una tabella con una
    colonna per campo: questi campi cambieranno (oggi percentuale e periodo,
    domani chissa'), e una migrazione di schema per aggiungere "vale solo il
    martedi'" non vale la pena su un archivio personale di dieci righe."""
    db = await get_db()
    async with db.execute("SELECT id, payload FROM discounts ORDER BY id") as cursor:
        rows = await cursor.fetchall()
    out: list[Discount] = []
    for saved_id, payload in rows:
        try:
            out.append(Discount(**{**orjson.loads(payload), "id": saved_id}))
        except (orjson.JSONDecodeError, ValueError):
            # Una tessera illeggibile non deve impedire di usare le altre.
            continue
    return out


async def save_discount(discount: Discount) -> Discount:
    db = await get_db()
    payload = orjson.dumps(discount.model_dump(mode="json", exclude={"id"})).decode()
    if discount.id:
        await db.execute("UPDATE discounts SET payload = ? WHERE id = ?", (payload, discount.id))
        saved_id = discount.id
    else:
        cursor = await db.execute("INSERT INTO discounts (payload) VALUES (?)", (payload,))
        saved_id = cursor.lastrowid
    await db.commit()
    return discount.model_copy(update={"id": saved_id})


async def delete_discount(discount_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM discounts WHERE id = ?", (discount_id,))
    await db.commit()
    return bool(cursor.rowcount)


async def active_discounts() -> list[Discount]:
    """Quelle da passare alla ricerca. Mai sollevare: senza tessere si cerca lo stesso."""
    try:
        return [d for d in await list_discounts() if d.active]
    except Exception:  # noqa: BLE001
        logger.warning("tessere non leggibili: la ricerca prosegue senza", exc_info=True)
        return []


async def get_preferences() -> dict[str, Any]:
    db = await get_db()
    async with db.execute("SELECT key, value FROM preferences") as cursor:
        rows = await cursor.fetchall()
    out: dict[str, Any] = {}
    for key, value in rows:
        if key not in KNOWN_PREFERENCES:
            continue
        try:
            out[key] = orjson.loads(value)
        except orjson.JSONDecodeError:
            continue
    return out


async def set_preferences(values: dict[str, Any]) -> dict[str, Any]:
    db = await get_db()
    for key, value in values.items():
        if key not in KNOWN_PREFERENCES:
            continue
        if value is None or value == "" or value == []:
            await db.execute("DELETE FROM preferences WHERE key = ?", (key,))
            continue
        await db.execute(
            "INSERT INTO preferences (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, orjson.dumps(value).decode()),
        )
    await db.commit()
    return await get_preferences()
