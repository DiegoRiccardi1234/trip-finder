"""Applicazione FastAPI: API di ricerca in streaming piu' frontend statico.

Un solo processo, un solo comando:

    .venv\\Scripts\\uvicorn app.main:app --reload

La ricerca e' esposta come Server-Sent Events perche' i risultati arrivano nel
giro di decine di secondi e non ha senso far fissare all'utente una pagina vuota
finche' l'ultimo provider non ha finito.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, time as time_type
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.geo.resolver import PlaceNotFound, get_resolver
from app.models import (
    BOOKABLE_MODES,
    CompareRequest,
    Discount,
    Mode,
    SavedSearchIn,
    SearchQuery,
    Weights,
)
from app.orchestrator import profile
from app.orchestrator import cache, circuit_breaker
from app.orchestrator.db import close_db
from app.orchestrator.search_service import SearchService
from app.providers import registry
from app.providers.browser_pool import close_browser_pool
from app.providers.http_client import close_http_client

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # L'indice geografico costa un paio di secondi: si carica all'avvio, non
    # alla prima ricerca, cosi' il primo utente non paga il ritardo.
    get_resolver()
    logger.info("provider disponibili: %d", len(registry.all_providers()))
    try:
        yield
    finally:
        await close_http_client()
        await close_browser_pool()
        await close_db()


app = FastAPI(title="Trip Finder", version="0.1.0", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/suggest")
async def suggest(q: str = Query(min_length=2), limit: int = 8) -> list[str]:
    return get_resolver().suggest(q, limit=limit)


@app.get("/api/providers")
async def providers() -> dict:
    return {
        "providers": [
            {
                "id": provider.id,
                "name": provider.name,
                "mode": provider.mode.value,
                "tier": provider.tier,
                "granularity": provider.granularity,
                "static": provider.is_static,
            }
            for provider in sorted(
                registry.all_providers(), key=lambda p: (p.mode.value, p.id)
            )
        ],
        "health": await circuit_breaker.snapshot(),
    }


@app.post("/api/providers/reset")
async def reset_providers(provider: str | None = None) -> dict:
    """Riapre i circuiti. Serve dopo aver sistemato un adapter."""
    await circuit_breaker.reset(provider)
    return {"ok": True}


@app.post("/api/cache/clear")
async def clear_cache(prefix: str | None = None) -> dict:
    removed = await cache.clear(prefix)
    return {"removed": removed}


@app.get("/api/search")
async def search(
    origin: str = Query(min_length=2),
    destination: str = Query(min_length=2),
    day: date = Query(alias="date"),
    pax: int = Query(1, ge=1, le=9),
    modes: str = Query("rail,bus,air,ferry"),
    bag: bool = False,
    budget: float | None = None,
    arrive_by: str | None = None,
    depart_after: str | None = None,
    allow_night: bool = True,
    max_changes: int = Query(3, ge=0, le=3),
    w_price: float = 1.0,
    w_duration: float = 1.0,
    w_risk: float = 0.8,
    w_night: float = 0.5,
    w_arrival: float = 0.6,
    w_co2: float = 0.0,
    origin_nodes: str | None = None,
    destination_nodes: str | None = None,
    use_discounts: bool = True,
) -> EventSourceResponse:
    selected = {
        Mode(value.strip())
        for value in modes.split(",")
        if value.strip() in {mode.value for mode in BOOKABLE_MODES}
    }
    if not selected:
        raise HTTPException(400, "nessun modo di trasporto selezionato")

    query = SearchQuery(
        origin=origin,
        destination=destination,
        date=day,
        pax=pax,
        modes=selected,
        with_checked_bag=bag,
        max_budget=budget,
        arrive_by=_parse_time(arrive_by),
        depart_after=_parse_time(depart_after),
        allow_night=allow_night,
        max_changes=max_changes,
        weights=Weights(
            price=w_price,
            duration=w_duration,
            risk=w_risk,
            night_bonus=w_night,
            arrival_penalty=w_arrival,
            co2=w_co2,
        ),
        origin_nodes=_id_set(origin_nodes),
        destination_nodes=_id_set(destination_nodes),
        # Le tessere si leggono qui e non piu' in basso: il costo si calcola
        # dentro la composizione, a decine di migliaia di chiamate per ricerca,
        # e non e' il posto per interrogare un database.
        discounts=await profile.active_discounts() if use_discounts else [],
    )

    async def stream() -> AsyncIterator[dict[str, str]]:
        async for event in SearchService().run(query):
            yield event.as_sse()

    return EventSourceResponse(stream())


@app.post("/api/parse")
async def parse_natural_language(text: str = Query(min_length=4)) -> dict:
    """Da "devo essere a Matera venerdi' sera" ai parametri della ricerca."""
    from app.ai.nl_query import ParseFailed, parse

    try:
        query = await parse(text)
    except ParseFailed as exc:
        raise HTTPException(422, str(exc)) from exc
    return query.model_dump(mode="json")


@app.get("/api/ai/status")
async def ai_status() -> dict:
    """Diagnostica: quali modelli sono utilizzabili adesso e quali penalizzati."""
    from app.ai import client, model_selector

    if not client.is_configured():
        return {"configured": False, "detail": "OPENROUTER_API_KEY non impostata"}
    return {
        "configured": True,
        "json": await model_selector.rank_models("json"),
        "advice": await model_selector.rank_models("advice"),
        "penalties": await model_selector.current_penalties(),
    }


@app.get("/api/resolve")
async def resolve(q: str = Query(min_length=2)) -> JSONResponse:
    """Diagnostica: mostra in quali fermate si traduce una localita'."""
    try:
        place = get_resolver().resolve(q)
    except PlaceNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return JSONResponse(place.model_dump(mode="json"))


@app.post("/api/advice/compare")
async def advice_compare(request: CompareRequest) -> dict:
    """Un consiglio solo su piu' possibilita', per dire quale conviene.

    Con due mete i due consigli per colonna non possono confrontarsi: ciascuno
    vede solo la propria classifica. Qui arrivano insieme."""
    from app.ai.advisor import compare

    try:
        text = await compare(request)
    except Exception:  # noqa: BLE001 - un consiglio mancante non rompe la pagina
        logger.debug("confronto non disponibile", exc_info=True)
        text = None
    return {"text": text}


@app.get("/api/profile")
async def profile_read() -> dict:
    """Preferenze, ricerche salvate e tessere, in una chiamata sola all'apertura."""
    from app.orchestrator import profile

    return {
        "preferences": await profile.get_preferences(),
        "searches": await profile.list_searches(),
        "discounts": [d.model_dump(mode="json") for d in await profile.list_discounts()],
    }


@app.post("/api/profile/discounts")
async def discount_save(discount: Discount) -> dict:
    """Aggiunge o aggiorna una tessera. Con `id` aggiorna, senza crea."""
    from app.orchestrator import profile

    saved = await profile.save_discount(discount)
    return saved.model_dump(mode="json")


@app.delete("/api/profile/discounts/{discount_id}")
async def discount_delete(discount_id: int) -> dict:
    from app.orchestrator import profile

    if not await profile.delete_discount(discount_id):
        raise HTTPException(404, "tessera non trovata")
    return {"deleted": discount_id}


@app.put("/api/profile/preferences")
async def profile_write(values: dict) -> dict:
    from app.orchestrator import profile

    return {"preferences": await profile.set_preferences(values)}


@app.post("/api/profile/searches")
async def profile_save(entry: SavedSearchIn) -> dict:
    from app.orchestrator import profile

    return await profile.save_search(entry.name.strip(), entry.params, entry.summary)


@app.delete("/api/profile/searches/{saved_id}")
async def profile_delete(saved_id: int) -> dict:
    from app.orchestrator import profile

    if not await profile.delete_search(saved_id):
        raise HTTPException(404, "ricerca salvata non trovata")
    return {"deleted": saved_id}


def _id_set(value: str | None) -> set[str]:
    """Id di fermata separati da virgola. Vuoto significa "tutte"."""
    if not value:
        return set()
    return {piece.strip() for piece in value.split(",") if piece.strip()}


def _parse_time(value: str | None) -> time_type | None:
    if not value:
        return None
    try:
        hours, minutes = value.split(":")[:2]
        return time_type(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
