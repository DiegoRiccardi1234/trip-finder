"""Circuit breaker per provider.

Uno scraper che ha appena preso tre 403 di fila non guarira' al quarto tentativo:
continuare a interrogarlo ruba secondi al budget della ricerca e alza il rischio
di farsi bloccare piu' a lungo. Qui si apre il circuito per qualche minuto e si
dichiara il provider fuori uso, in modo visibile nella UI.

Lo scope separa i fallimenti per contesto: un adapter puo' funzionare sulle
tratte nazionali e rompersi su quelle internazionali, e non ha senso spegnerlo
del tutto per colpa di una rotta.
"""

from __future__ import annotations

import logging
import time

from app.orchestrator.db import get_db

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
#: Backoff progressivo in secondi, indicizzato sul numero di aperture.
OPEN_SECONDS = [300.0, 600.0, 1800.0, 3600.0]

#: Lo stato che il servizio registra quando un provider non ha risposto in tempo.
TIMEOUT = "timeout"

#: Per i timeout la prima pausa e' molto piu' corta. Un rifiuto e un ritardo non
#: sono la stessa cosa: il primo e' il sito che ci respinge, e insistere peggiora
#: la posizione; il secondo e' il sito che ci ha messo troppo, e la colpa puo'
#: essere nostra — una rete lenta, una ricerca affollata, un adapter che chiede
#: troppe pagine in fila. Trattarli uguale e' costato caro il 2026-08-17: la
#: paginazione di Trenitalia sforava i 18 s di budget, tre ricerche di fila
#: spegnevano l'operatore per cinque minuti, e chi cercava un treno non ne
#: vedeva **nessuno**. Da qui in poi le pause si riallineano: la clemenza vale
#: per l'episodio, non per l'abitudine.
OPEN_SECONDS_TIMEOUT = [60.0, 300.0, 1800.0, 3600.0]

GLOBAL_SCOPE = "*"


def pausa_dopo(fails: int, status: str) -> float:
    """Per quanti secondi si smette di interrogare, dopo questo fallimento.

    Zero significa che il circuito resta chiuso e si continua a chiedere: due
    fallimenti sono sfortuna, non un guasto."""
    if fails < FAILURE_THRESHOLD:
        return 0.0
    scala = OPEN_SECONDS_TIMEOUT if status == TIMEOUT else OPEN_SECONDS
    step = min((fails - FAILURE_THRESHOLD) // FAILURE_THRESHOLD, len(scala) - 1)
    return scala[step]


async def is_open(provider: str, scope: str = GLOBAL_SCOPE) -> tuple[bool, str | None]:
    """Ritorna (aperto, motivo). Aperto significa: non interrogare."""
    db = await get_db()
    async with db.execute(
        "SELECT opened_until, last_detail FROM provider_health "
        "WHERE provider = ? AND scope IN (?, ?) ORDER BY opened_until DESC LIMIT 1",
        (provider, scope, GLOBAL_SCOPE),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return False, None
    opened_until, detail = row
    if opened_until > time.time():
        remaining = int(opened_until - time.time())
        return True, f"{detail or 'fallimenti ripetuti'} (riprova fra {remaining}s)"
    return False, None


async def record_success(provider: str, scope: str = GLOBAL_SCOPE) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO provider_health(provider, scope, fails, opened_until, "
        "last_status, updated_at) VALUES (?, ?, 0, 0, 'ok', ?) "
        "ON CONFLICT(provider, scope) DO UPDATE SET fails = 0, opened_until = 0, "
        "last_status = 'ok', last_detail = NULL, updated_at = excluded.updated_at",
        (provider, scope, time.time()),
    )
    await db.commit()


async def record_failure(
    provider: str, status: str, detail: str | None = None, scope: str = GLOBAL_SCOPE
) -> bool:
    """Registra un fallimento. Ritorna True se il circuito e' stato aperto."""
    db = await get_db()
    async with db.execute(
        "SELECT fails FROM provider_health WHERE provider = ? AND scope = ?",
        (provider, scope),
    ) as cursor:
        row = await cursor.fetchone()
    fails = (row[0] if row else 0) + 1

    pausa = pausa_dopo(fails, status)
    opened_until = time.time() + pausa if pausa else 0.0

    await db.execute(
        "INSERT INTO provider_health(provider, scope, fails, opened_until, "
        "last_status, last_detail, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(provider, scope) DO UPDATE SET fails = excluded.fails, "
        "opened_until = excluded.opened_until, last_status = excluded.last_status, "
        "last_detail = excluded.last_detail, updated_at = excluded.updated_at",
        (provider, scope, fails, opened_until, status, detail, time.time()),
    )
    await db.commit()

    if opened_until:
        logger.warning(
            "circuito aperto per %s (%s): %d fallimenti, ultimo %s",
            provider,
            scope,
            fails,
            status,
        )
        return True
    return False


async def reset(provider: str | None = None) -> None:
    db = await get_db()
    if provider:
        await db.execute("DELETE FROM provider_health WHERE provider = ?", (provider,))
    else:
        await db.execute("DELETE FROM provider_health")
    await db.commit()


async def snapshot() -> list[dict[str, object]]:
    """Stato corrente di tutti i provider, per la diagnostica in UI."""
    db = await get_db()
    async with db.execute(
        "SELECT provider, scope, fails, opened_until, last_status, last_detail "
        "FROM provider_health ORDER BY provider, scope"
    ) as cursor:
        rows = await cursor.fetchall()
    now = time.time()
    return [
        {
            "provider": provider,
            "scope": scope,
            "fails": fails,
            "open": opened_until > now,
            "open_for_s": max(0, int(opened_until - now)),
            "last_status": last_status,
            "last_detail": last_detail,
        }
        for provider, scope, fails, opened_until, last_status, last_detail in rows
    ]
