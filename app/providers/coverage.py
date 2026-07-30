"""Quali tratte serve davvero un operatore, tenuto in cache a lungo.

E' la contromisura piu' efficace all'esplosione delle richieste. Con venticinque
adapter, ogni ricerca proporrebbe centinaia di interrogazioni, e la maggior parte
per tratte che quell'operatore non ha mai servito: Renfe non vola da Torino a
Matera, Itabus non passa da Bolzano.

Molti operatori pubblicano l'elenco delle proprie rotte in un endpoint separato,
stabile e piccolo (Ryanair lo fa per aeroporto, Itabus lo porta dentro l'elenco
fermate, Marino ha `get_possible_routes`). Si scarica una volta al mese e poi le
tratte impossibili costano zero richieste.

Tre esiti, e la differenza conta:
  - `True`  -> serve la tratta, si procede;
  - `False` -> non la serve, si salta senza spendere nulla;
  - `None`  -> non lo sappiamo, si prova. L'ignoranza non deve mai diventare
    un rifiuto silenzioso: un operatore di cui non conosciamo le rotte deve
    essere interrogato, non escluso.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Iterable

from app.orchestrator import cache

logger = logging.getLogger(__name__)

#: Le rotte cambiano con gli orari stagionali, non di giorno in giorno.
TTL_SECONDS = 30 * 86400


def _key(provider_id: str, origin_key: str) -> str:
    # Chiave leggibile e non hashata, cosi' `forget()` puo' invalidare per
    # prefisso la copertura di un solo operatore dopo averne sistemato l'adapter.
    safe = "".join(ch for ch in origin_key.upper() if ch.isalnum() or ch in "-_")[:48]
    return f"coverage:{provider_id}:{safe}"


async def remember(
    provider_id: str,
    origin_key: str,
    destinations: Iterable[str],
    *,
    allow_empty: bool = False,
) -> None:
    """Registra le destinazioni raggiungibili da un punto di partenza.

    `allow_empty` esiste per un motivo preciso. Per quasi tutti gli operatori un
    elenco vuoto significa "il dato non e' arrivato", e memorizzarlo
    equivarrebbe a dichiarare che quell'operatore non serve piu' niente: da quel
    momento sparirebbe da ogni ricerca senza un solo errore visibile. Solo dove
    il vuoto e' una risposta vera (Ryanair risponde 404 per gli scali che non
    serve) ha senso salvarlo."""
    values = sorted({str(d).upper() for d in destinations if d})
    if not values and not allow_empty:
        logger.debug(
            "copertura vuota per %s da %s: non la memorizzo, resta ignota",
            provider_id,
            origin_key,
        )
        return
    await cache.set(_key(provider_id, origin_key), values, ttl=TTL_SECONDS)


async def destinations_from(provider_id: str, origin_key: str) -> list[str] | None:
    return await cache.get(_key(provider_id, origin_key))


async def serves(provider_id: str, origin_key: str, dest_key: str) -> bool | None:
    known = await destinations_from(provider_id, origin_key)
    if known is None:
        return None
    return dest_key.upper() in known


async def ensure(
    provider_id: str,
    origin_key: str,
    loader: Callable[[], Awaitable[Iterable[str]]],
    *,
    allow_empty: bool = False,
) -> list[str] | None:
    """Restituisce le destinazioni note, caricandole una volta sola se mancano.

    Se il caricamento fallisce si torna `None`: significa "non lo sappiamo", e
    a valle si prova comunque. Un elenco rotte irraggiungibile non deve
    trasformarsi in un operatore che sembra non coprire piu' nulla."""
    known = await destinations_from(provider_id, origin_key)
    if known is not None:
        return known
    try:
        destinations = list(await loader())
    except Exception as exc:  # noqa: BLE001 - la copertura e' un'ottimizzazione
        logger.debug("copertura di %s da %s non disponibile: %s", provider_id, origin_key, exc)
        return None

    values = sorted({str(d).upper() for d in destinations if d})
    if not values and not allow_empty:
        return None  # ignoto, non "non serve nulla"
    await remember(provider_id, origin_key, values, allow_empty=allow_empty)
    return values


async def forget(provider_id: str | None = None) -> int:
    """Invalida la copertura, di un operatore o di tutti."""
    prefix = f"coverage:{provider_id}:" if provider_id else "coverage:"
    return await cache.clear(prefix)
