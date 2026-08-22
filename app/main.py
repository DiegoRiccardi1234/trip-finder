"""Applicazione FastAPI: API di ricerca in streaming piu' frontend statico.

Un solo processo, un solo comando:

    .venv\\Scripts\\uvicorn app.main:app --reload

La ricerca e' esposta come Server-Sent Events perche' i risultati arrivano nel
giro di decine di secondi e non ha senso far fissare all'utente una pagina vuota
finche' l'ultimo provider non ha finito.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, time as time_type
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from app import config
from app.config import get_settings
from app.geo.resolver import PlaceNotFound, get_resolver
from app.models import (
    BOOKABLE_MODES,
    AdviceRequest,
    ChatRequest,
    ParseRequest,
    CompareRequest,
    Discount,
    Mode,
    SavedSearchIn,
    SearchQuery,
    TripAdviceRequest,
    Weights,
)
from app.orchestrator import profile
from app.orchestrator import cache, circuit_breaker
from app.orchestrator.db import close_db
from app.orchestrator.search_service import SearchService
from app.providers import registry
from app.providers.browser_pool import close_browser_pool
from app.providers.http_client import close_http_client
from app.version import VERSION

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

    # Il catalogo delle tessere si riscarica in sottofondo, una volta al giorno.
    # Non blocca l'avvio: e' un dato di comodo, e far aspettare la pagina per
    # una percentuale sarebbe sproporzionato. Uno zip di agosto porterebbe
    # altrimenti il catalogo di agosto per sempre, mentre il 30 novembre
    # scadono due promo.
    async def _tessere() -> None:
        from app.routing import rates, tessere

        try:
            await tessere.aggiorna()
        except Exception:  # noqa: BLE001 - mai far cadere l'avvio per questo
            logger.debug("aggiornamento del catalogo tessere non riuscito", exc_info=True)
        try:
            # I cambi servono a `cost.compute`, che e' sincrona e gira decine di
            # migliaia di volte per ricerca: si scaricano qui, una volta.
            await rates.aggiorna()
        except Exception:  # noqa: BLE001 - senza cambi si continua a non convertire
            logger.debug("cambi non aggiornati", exc_info=True)

    compito = asyncio.create_task(_tessere())
    try:
        yield
    finally:
        compito.cancel()
        await close_http_client()
        await close_browser_pool()
        await close_db()


app = FastAPI(title="Trip Finder", version=VERSION, lifespan=lifespan)


def _asset_token() -> str:
    """Un'impronta di CSS e JS, da appendere ai loro indirizzi.

    Serve a rompere la cache del browser quando cambiano. Senza, il browser si
    tiene il foglio di stile vecchio e non chiede nemmeno: le regole nuove non
    arrivano mai, e il difetto che ne esce sembra un difetto del codice — una
    pagina con tutte le sezioni impilate e le linguette che non rispondono.
    Succede solo a chi la pagina l'aveva gia' aperta prima, cioe' mai a un
    browser di prova appena avviato: e' un difetto che le prove automatiche non
    possono vedere, e infatti non l'hanno visto."""
    ultimo = 0
    for nome in ("style.css", "app.js"):
        try:
            ultimo = max(ultimo, (STATIC_DIR / nome).stat().st_mtime_ns)
        except OSError:
            pass
    return f"{VERSION}-{ultimo // 1_000_000 % 1_000_000_000}"


@app.get("/")
async def index() -> HTMLResponse:
    """La pagina, con l'impronta di CSS e JS gia' scritta dentro."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("__ASSETS__", _asset_token()),
        # La pagina e' minuscola e cambia a ogni rilascio: rileggerla ogni volta
        # non costa niente e garantisce che l'impronta arrivi aggiornata.
        headers={"Cache-Control": "no-store"},
    )


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
async def parse_natural_language(request: ParseRequest) -> dict:
    """Da "devo essere a Matera venerdi' sera" ai parametri della ricerca.

    Risponde sempre con un viaggio, che di tappe puo' averne una o quattro:
    "da Matera a Roma per tre giorni, poi a Torino" e' una frase sola.

    **Oppure con una domanda.** Se manca uno dei tre dati senza cui non si puo'
    cercare — da dove, dove, quando — la risposta e' `{"domanda": "..."}` con
    stato 200, perche' non e' un fallimento: e' un turno. Prima quel buco lo
    riempiva il modello di sua iniziativa, quasi sempre con la data di oggi, e
    la ricerca partiva su un giorno che nessuno aveva chiesto. Una supposizione
    sbagliata non si vede; una domanda si'."""
    from app.ai.nl_query import ParseFailed, ServeAltro, parse

    storia = [
        {"role": messaggio.role, "content": messaggio.content}
        for messaggio in request.messages
    ]
    try:
        plan = await parse(request.text, history=storia or None)
    except ServeAltro as exc:
        return {"domanda": exc.domanda}
    except ParseFailed as exc:
        raise HTTPException(422, str(exc)) from exc
    return plan.model_dump(mode="json")


@app.post("/api/advice/trip")
async def advice_trip(request: TripAdviceRequest) -> dict:
    """Un consiglio sul viaggio **intero**, non sulle sue tappe una per una.

    Le tappe si consigliano da sole in fondo a ciascuna colonna. Quello che li'
    non si puo' dire e' come stanno insieme: se conviene spostare la sosta, se
    una tappa costa quanto tutte le altre, se un notturno in mezzo rovina il
    giorno dopo."""
    from app.ai import client
    from app.ai.advisor import advise_trip

    try:
        answer = await advise_trip(request)
    except Exception:  # noqa: BLE001 - un consiglio mancante non rompe la pagina
        logger.debug("consiglio sul viaggio non disponibile", exc_info=True)
        answer = client.Answer(reason=client.FAILED)
    return {"text": answer.text or None, "reason": answer.reason}


@app.get("/api/health")
async def health() -> dict:
    """Risponde e basta, e dice quale versione sta rispondendo.

    Serve alla pagina che aspetta il riavvio dopo un aggiornamento: deve poter
    chiedere «ci sei?» molte volte senza far lavorare nessuno, e riconoscere che
    a rispondere e' la versione nuova e non quella vecchia che non era ancora
    morta."""
    return {"ok": True, "version": VERSION}


@app.get("/api/update/check")
async def update_check() -> dict:
    """Che versione c'e' installata e se ne e' uscita una piu' nuova."""
    from app import update

    return await update.check()


@app.post("/api/update/install")
async def update_install() -> dict:
    """Scarica la versione nuova, passa la mano all'aggiornatore e si spegne."""
    from app import update

    return await update.install()


@app.get("/api/ai/keys/status")
async def ai_keys_status() -> dict:
    """Quali fornitori hanno una chiave — **non** quali chiavi.

    Ritorna booleani: una chiave che esce da qui una volta e' una chiave che
    finisce in una schermata, in un log del browser o in uno screenshot."""
    from app.ai import providers

    settings = get_settings()
    dal_file = set(config.read_local_secrets())
    return {
        "providers": [
            {
                "name": provider.name,
                "free": provider.free,
                "note": provider.label,
                "needs_key": provider.needs_key,
                "configured": providers.is_configured(provider),
                # Chi ha messo la chiave nel `.env` non deve vedersela
                # "cancellare" da un campo vuoto che non ha mai riempito.
                "from_file": provider.key_field in dal_file,
            }
            for provider in providers.PROVIDERS
        ],
        "allow_paid": settings.allow_paid_providers,
        "llm_provider": settings.llm_provider,
    }


@app.post("/api/ai/keys")
async def ai_keys_save(values: dict[str, object]) -> dict:
    """Salva le chiavi e le rende valide **subito**.

    `get_settings` e' in cache per tutto il processo: senza svuotarla una chiave
    appena inserita varrebbe solo dal riavvio successivo, e da fuori sembrerebbe
    semplicemente non funzionare."""
    config.save_local_secrets(values)
    get_settings.cache_clear()
    return await ai_keys_status()


#: I due compiti, con il nome che ha senso per chi legge.
COMPITI_IA = {
    "json": "Interpretazione della frase",
    "advice": "Consiglio",
}


@app.get("/api/ai/models")
async def ai_models() -> dict:
    """I modelli disponibili per ciascun compito, con salute e penalita'.

    E' quello che serve per scegliere: quali ci sono, quali sono vivi adesso,
    quali hanno appena sbagliato, e quale userebbe se non scegliessi tu."""
    from app.ai import client, model_selector

    if not client.is_configured():
        return {"configured": False, "tasks": {}}

    settings = get_settings()
    tasks: dict[str, object] = {}
    for compito, etichetta in COMPITI_IA.items():
        ordine = [
            f"{provider.name}/{model}"
            for provider, model in await model_selector.rank_candidates(compito)
        ]
        tasks[compito] = {
            "label": etichetta,
            "pinned": str(getattr(settings, f"{compito}_model", "") or ""),
            # Chi userebbe se non scegliessi: si dice sempre, anche quando una
            # scelta c'e', perche' e' il modello su cui si ripiega.
            "auto": ordine[0] if ordine else "",
            "order": ordine,
            "candidates": await model_selector.catalogo(compito),
        }
    return {"configured": True, "tasks": tasks}


@app.get("/api/ai/status")
async def ai_status() -> dict:
    """Diagnostica: quali fornitori e modelli sono utilizzabili adesso."""
    from app.ai import client, model_selector, providers

    usable = providers.configured()
    if not client.is_configured():
        return {
            "configured": False,
            "detail": "nessuna chiave impostata: vedi .env.example",
            "known": [provider.name for provider in providers.PROVIDERS],
        }

    async def order(task: str) -> list[str]:
        return [
            f"{provider.name}/{model}"
            for provider, model in await model_selector.rank_candidates(task)
        ]

    return {
        "configured": True,
        "providers": [
            {"name": provider.name, "free": provider.free, "note": provider.label}
            for provider in usable
        ],
        "json": await order("json"),
        "advice": await order("advice"),
        "penalties": {
            provider.name: await model_selector.current_penalties(provider.name)
            for provider in usable
        },
    }


@app.get("/api/resolve")
async def resolve(q: str = Query(min_length=2)) -> JSONResponse:
    """In quali fermate si traduce una localita'. La usa il modulo di ricerca.

    L'ordine qui e' quello di **presentazione**, con la distanza allegata: il
    motore non passa da questo endpoint (`search_service` chiama il resolver
    direttamente) e continua a usare `place.nodes` in ordine di punteggio.
    Riordinare qui, e non nella pagina, tiene la regola in Python dove la suite
    la puo' provare, e la rende impossibile da confondere con la selezione."""
    try:
        place = get_resolver().resolve(q)
    except PlaceNotFound as exc:
        raise HTTPException(404, str(exc)) from exc

    payload = place.model_dump(mode="json")
    payload["nodes"] = [
        {**node.model_dump(mode="json"), "km": round(km, 1)}
        for node, km in place.fermate_da_mostrare()
    ]
    return JSONResponse(payload)


@app.post("/api/advice")
async def advice(request: AdviceRequest) -> dict:
    """Il consiglio su una classifica sola, chiesto dalla pagina.

    Prima nasceva dentro la ricerca e viaggiava con l'evento `advice`. Cosi'
    pero' il consiglio vedeva quello che vedeva il **motore**, non quello che
    vede chi guarda: su un'andata e ritorno commentava i prezzi della sola
    andata sotto schede che mostravano il totale, e numerava soluzioni che a
    schermo non avevano numero. Chiedendolo dalla pagina il problema non si
    corregge: non esiste. In piu' e' l'unico modo per avere un «Riprova» che
    faccia davvero qualcosa."""
    from app.ai import client
    from app.ai.advisor import advise

    try:
        answer = await advise(request)
    except Exception:  # noqa: BLE001 - un consiglio mancante non rompe la pagina
        logger.debug("consiglio non disponibile", exc_info=True)
        answer = client.Answer(reason=client.FAILED)
    return {"text": answer.text or None, "reason": answer.reason}


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict:
    """Una domanda sulla classifica che si sta guardando.

    E' il consiglio che continua, non una funzione nuova: stessa classifica,
    stessi vincoli, stessi riferimenti cliccabili. La differenza e' che qui la
    conversazione ha una storia, e che il Profilo entra finalmente nel prompt
    invece di arrivare solo di rimbalzo attraverso i campi del modulo.

    Il modello non lancia ricerche e non tocca il modulo. E' un limite voluto:
    puo' parlare solo di cio' che ha davanti, quindi non puo' inventare un
    collegamento che non e' stato trovato."""
    from app.ai import client
    from app.ai.advisor import chat as rispondi

    try:
        answer = await rispondi(request)
    except Exception:  # noqa: BLE001 - una risposta mancante non rompe la pagina
        logger.debug("risposta in chat non disponibile", exc_info=True)
        answer = client.Answer(reason=client.FAILED)
    return {"text": answer.text or None, "reason": answer.reason}


@app.post("/api/advice/compare")
async def advice_compare(request: CompareRequest) -> dict:
    """Un consiglio solo su piu' possibilita', per dire quale conviene.

    Con due mete i due consigli per colonna non possono confrontarsi: ciascuno
    vede solo la propria classifica. Qui arrivano insieme."""
    from app.ai import client
    from app.ai.advisor import compare

    try:
        answer = await compare(request)
    except Exception:  # noqa: BLE001 - un consiglio mancante non rompe la pagina
        logger.debug("confronto non disponibile", exc_info=True)
        answer = client.Answer(reason=client.FAILED)
    # Il motivo viaggia accanto al testo: senza, la pagina riceveva `null` e non
    # aveva modo di distinguere "IA spenta" da "IA rifiutata".
    return {"text": answer.text or None, "reason": answer.reason}


@app.get("/api/tessere")
async def tessere_catalogo() -> dict:
    """Il catalogo delle tessere e delle riduzioni, con le loro scadenze.

    Le scadute restano nell'elenco, marcate: la Carta Verde non si compra piu'
    dal 4 aprile 2026, ma chi ce l'ha la usa fino alla scadenza sua, e toglierla
    vorrebbe dire non fargliela dichiarare."""
    from app.routing import tessere

    oggi = date.today()
    return {
        "generato_il": tessere.aggiornato_il(),
        "tessere": [
            {
                **voce.as_dict(),
                "scaduta": voce.scaduta(oggi),
                "non_ancora_valida": voce.non_ancora_valida(oggi),
                "stantia": voce.stantia(oggi),
            }
            for voce in tessere.tutte()
        ],
    }


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


class _StaticiRivalidati(StaticFiles):
    """File statici che il browser deve **ricontrollare** ogni volta.

    `StaticFiles` manda `ETag` e `Last-Modified` ma nessun `Cache-Control`, e
    senza quello il browser applica una cache euristica: si tiene il file per un
    pezzo senza chiedere se e' cambiato. In locale una richiesta di controllo
    costa niente e finisce quasi sempre in un `304`, mentre un foglio di stile
    vecchio costa una pagina rotta."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        risposta = super().file_response(*args, **kwargs)
        risposta.headers["Cache-Control"] = "no-cache"
        return risposta


if STATIC_DIR.exists():
    app.mount("/static", _StaticiRivalidati(directory=STATIC_DIR), name="static")
