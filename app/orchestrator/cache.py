"""Cache su SQLite con TTL differenziati per tipo di dato.

I prezzi cambiano di continuo, gli orari quasi mai e la mappatura di una fermata
sull'ID di un operatore praticamente mai. Usare lo stesso TTL per tutto
significa o dati vecchi o richieste inutili.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

import orjson

from app.orchestrator.db import get_db

logger = logging.getLogger(__name__)

#: Secondi di validita' per famiglia di dato.
TTL: dict[str, float] = {
    "fare": 15 * 60,  # prezzi e disponibilita'
    "schedule": 6 * 3600,  # orari senza prezzo
    "resolve": 30 * 86400,  # fermata -> ID nativo del provider
    "static": 30 * 86400,  # tabelle raramente mutevoli
    "model_health": 5 * 60,  # salute endpoint OpenRouter
    "ai": 3600,  # risposte IA deterministiche a parita' di input
}
DEFAULT_TTL = 900.0


def make_key(namespace: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"{namespace}:{digest}"


async def get(key: str) -> Any | None:
    db = await get_db()
    async with db.execute(
        "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    value, expires_at = row
    if expires_at < time.time():
        await db.execute("DELETE FROM cache WHERE key = ?", (key,))
        await db.commit()
        return None
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError:
        return None


async def set(key: str, value: Any, kind: str = "fare", ttl: float | None = None) -> None:
    db = await get_db()
    seconds = ttl if ttl is not None else TTL.get(kind, DEFAULT_TTL)
    await db.execute(
        "INSERT INTO cache(key, value, expires_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "expires_at = excluded.expires_at",
        (key, orjson.dumps(value), time.time() + seconds),
    )
    await db.commit()


async def purge_expired() -> int:
    db = await get_db()
    cursor = await db.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
    await db.commit()
    return cursor.rowcount or 0


async def clear(prefix: str | None = None) -> int:
    db = await get_db()
    if prefix:
        cursor = await db.execute("DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",))
    else:
        cursor = await db.execute("DELETE FROM cache")
    await db.commit()
    return cursor.rowcount or 0
