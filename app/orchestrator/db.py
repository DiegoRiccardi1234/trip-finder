"""Connessione SQLite condivisa da cache e circuit breaker.

Un solo file, un solo processo, una sola connessione in WAL. Non serve un pool:
il carico e' una manciata di scritture per ricerca.
"""

from __future__ import annotations

import asyncio
import logging

import aiosqlite

from app.config import get_settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cache_expires ON cache(expires_at);

CREATE TABLE IF NOT EXISTS provider_health (
    provider     TEXT NOT NULL,
    scope        TEXT NOT NULL,
    fails        INTEGER NOT NULL DEFAULT 0,
    opened_until REAL NOT NULL DEFAULT 0,
    last_status  TEXT,
    last_detail  TEXT,
    updated_at   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, scope)
);

CREATE TABLE IF NOT EXISTS model_penalty (
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    penalty    REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (provider, model)
);

CREATE TABLE IF NOT EXISTS search_history (
    id         TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    query      TEXT NOT NULL,
    summary    TEXT
);

-- Il profilo sta qui e non nel browser: pulire i dati di navigazione o aprire
-- l'app da un altro browser non deve far sparire le ricerche salvate.
CREATE TABLE IF NOT EXISTS saved_searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    params     TEXT NOT NULL,   -- la query string della pagina: si riapre tale e quale
    summary    TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Tessere, abbonamenti e convenzioni. Un totale porta-a-porta che ignora la
-- tessera che uno ha in tasca sbaglia la classifica, non solo la cifra.
CREATE TABLE IF NOT EXISTS discounts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL   -- il Discount serializzato: i campi cambieranno
);
"""

_connection: aiosqlite.Connection | None = None
_loop: asyncio.AbstractEventLoop | None = None
_lock: asyncio.Lock | None = None


async def get_db() -> aiosqlite.Connection:
    """Connessione condivisa, legata all'event loop corrente.

    aiosqlite fa girare la connessione su un thread proprio e ne coordina l'uso
    attraverso l'event loop che l'ha creata. Riusarla da un loop diverso non da'
    errore: si blocca e basta. Succede a ogni test asincrono, perche' pytest ne
    crea uno nuovo per ciascuno. Qui la si riapre quando il loop cambia, e la
    vecchia viene abbandonata.

    Chi apre deve chiudere, e qui non e' una formalita': dalla versione 0.20
    aiosqlite serve la connessione con un thread interno **non** demone
    (`_connection_worker_thread`), che non si puo' marcare dall'esterno. Finche'
    resta vivo il processo non termina. Nella suite si vedeva come un blocco
    inspiegabile: tutti i test passavano e poi pytest non usciva piu'. Gli
    script chiamano `close_db()` in chiusura; per i test ci pensa un hook in
    `tests/conftest.py`."""
    global _connection, _loop, _lock

    current = asyncio.get_running_loop()
    if _connection is not None and _loop is current:
        return _connection

    # Anche il lock appartiene a un loop: quello in cui viene atteso la prima
    # volta. Riusarlo da un loop diverso e' l'errore che stiamo evitando con la
    # connessione, quindi quando il loop cambia si rifa' anche lui.
    if _lock is None or _loop is not current:
        _lock = asyncio.Lock()

    async with _lock:
        if _connection is not None and _loop is not current:
            logger.debug("event loop cambiato: riapro la connessione a SQLite")
            _connection = None
        if _connection is None:
            settings = get_settings()
            settings.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(settings.db_path)
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA synchronous=NORMAL")
            await connection.executescript(SCHEMA)
            await connection.commit()
            _connection = connection
            _loop = current
            logger.debug("SQLite pronto: %s", settings.db_path)
    return _connection  # type: ignore[return-value]


async def close_db() -> None:
    global _connection, _loop
    if _connection is not None:
        try:
            await _connection.close()
        except Exception:  # noqa: BLE001 - in chiusura non c'e' nulla da salvare
            pass
        _connection = None
        _loop = None
