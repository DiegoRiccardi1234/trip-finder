"""Apertura SQLite concorrente: un solo worker, anche al primo accesso."""
import asyncio
from types import SimpleNamespace

import pytest

from app.orchestrator import db


@pytest.mark.asyncio
async def test_first_concurrent_access_opens_only_one_connection(tmp_path, monkeypatch):
    await db.close_db()
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(db_path=tmp_path / "cache.sqlite3"))
    original = db.aiosqlite.connect
    opened = []

    async def connect(*args, **kwargs):
        # Obbliga tutti i chiamanti a presentarsi prima che l'apertura finisca.
        await asyncio.sleep(0.01)
        connection = await original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(db.aiosqlite, "connect", connect)
    try:
        connections = await asyncio.gather(*(db.get_db() for _ in range(20)))
        assert len(opened) == 1
        assert all(connection is opened[0] for connection in connections)
        cursor = await connections[0].execute("SELECT COUNT(*) FROM cache")
        assert await cursor.fetchone() == (0,)
        await cursor.close()
    finally:
        await db.close_db()
        for connection in opened:
            await connection.close()


@pytest.mark.asyncio
async def test_failed_initialization_closes_connection_and_allows_retry(tmp_path, monkeypatch):
    await db.close_db()
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(db_path=tmp_path / "cache.sqlite3"))
    schema = db.SCHEMA
    monkeypatch.setattr(db, "SCHEMA", "THIS IS NOT SQL")
    with pytest.raises(db.aiosqlite.OperationalError):
        await db.get_db()
    assert db._connection is None
    monkeypatch.setattr(db, "SCHEMA", schema)
    connection = await db.get_db()
    assert connection is db._connection
    await db.close_db()
