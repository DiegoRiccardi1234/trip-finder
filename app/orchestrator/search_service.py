"""Orchestrazione di una ricerca, con risultati in streaming.

Una ricerca europea multimodale puo' richiedere decine di interrogazioni a siti
diversi, alcuni lenti, alcuni bloccati, alcuni rotti. Tre principi guidano
questo modulo:

1. **Niente attesa cieca.** I risultati escono man mano che arrivano; il primo
   itinerario compare dopo un paio di secondi, non alla fine.
2. **Budget rigido.** La ricerca finisce entro il tempo previsto anche se meta'
   dei provider non ha risposto. Chi non ce l'ha fatta viene dichiarato.
3. **Degrado, non fallimento.** Un adapter rotto toglie le sue opzioni e basta;
   non deve togliere le altre ne' far fallire la richiesta.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.config import get_settings
from app.geo.resolver import PlaceNotFound, get_resolver
from app.models import (
    Itinerary,
    Leg,
    Place,
    ProviderReport,
    ProviderStatus,
    SearchQuery,
    jsonable,
)
from app.orchestrator import circuit_breaker
from app.providers import known_routes
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.http_client import Blocked, get_http_client
from app.routing import composer, ranker

logger = logging.getLogger(__name__)

#: Quanti itinerari mostrare. Oltre questa soglia nessuno legge piu'.
TOP_N = 25
#: Ogni quanto ricalcolare la classifica mentre arrivano gambe nuove.
REFRESH_SECONDS = 1.2

#: Dove sappiamo interrogare qualcuno. Non e' un vincolo del motore — le citta'
#: del mondo si risolvono, gli scali si generano, i fusi sono giusti — e' l'elenco
#: dei paesi in cui esiste almeno un adapter oltre a Ryanair.
PAESI_COPERTI = frozenset(
    {"IT", "FR", "DE", "AT", "CH", "ES", "PT", "NL", "BE", "LU", "GB", "IE",
     "DK", "SE", "NO", "FI", "PL", "CZ", "SK", "HU", "SI", "HR", "RO", "BG",
     "GR", "EE", "LV", "LT", "RS", "BA", "ME", "MK", "AL", "LI", "MC", "SM",
     "MT", "CY", "MA", "TN", "TR", "UA"}
)


def _fuori_copertura(origin: Place, destination: Place) -> str:
    """Perche' non c'e' niente, detto in modo che si capisca cosa fare.

    «Nessun operatore copre questa tratta» era vero e inutile: chi cerca Tokyo
    non ha sbagliato a scrivere, e non ha modo di sapere che il problema non e'
    la sua ricerca ma la nostra copertura. Dirlo e' la stessa regola che vale
    per un operatore che non risponde: cio' che manca si dichiara."""
    fuori = [
        place.label
        for place in (origin, destination)
        if place.country and place.country not in PAESI_COPERTI
    ]
    if not fuori:
        return "nessun operatore copre questa tratta con i modi scelti"
    dove = " e ".join(fuori)
    return (
        f"Nessuno degli operatori che sappiamo interrogare arriva a {dove}. "
        "La ricerca copre l'Europa: fuori, per ora, ci sono solo i voli Ryanair. "
        "Gli orari e i prezzi di treni e pullman locali vanno cercati sui siti "
        "degli operatori del posto."
    )


@dataclass
class SearchEvent:
    event: str
    data: Any

    def as_sse(self) -> dict[str, str]:
        import orjson

        return {"event": self.event, "data": orjson.dumps(jsonable(self.data)).decode()}


@dataclass
class _State:
    legs_by_hop: dict[tuple[str, str], list[Leg]] = field(
        default_factory=lambda: defaultdict(list)
    )
    reports: dict[str, ProviderReport] = field(default_factory=dict)
    total_legs: int = 0
    pending_fast: int = 0
    pending_slow: int = 0
    #: Itinerari gia' mostrati: serve a marcare come nuovi quelli che arrivano
    #: nell'ondata lenta, invece di rimescolare la lista senza dirlo.
    seen_itineraries: set[str] = field(default_factory=set)
    #: Vero quando nessuna soluzione rispettava i vincoli e la classifica e'
    #: stata riempita con quelle fuori vincolo. Falso quasi sempre. *Quali*
    #: vincoli dire lo si decide sulle schede effettivamente spedite, non qui.
    constraints_dropped: bool = False


class SearchService:
    """Cerca e basta: il consiglio non nasce piu' qui.

    Lo chiede la pagina a ricerca finita (`POST /api/advice`), con le soluzioni
    come le sta mostrando. Da qui dentro il consiglio vedeva le classifiche del
    motore — due separate su un'andata e ritorno, dove a schermo la scheda e'
    una sola con il prezzo sommato — e numerava soluzioni che a schermo non
    avevano numero."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def run(self, query: SearchQuery) -> AsyncIterator[SearchEvent]:
        search_id = uuid.uuid4().hex[:12]
        deadline = time.monotonic() + self._settings.search_time_budget
        yield SearchEvent("started", {"search_id": search_id, "query": query})

        resolver = get_resolver()
        try:
            origin = resolver.resolve(query.origin, modes=set(query.modes))
            destination = resolver.resolve(query.destination, modes=set(query.modes))
        except PlaceNotFound as exc:
            yield SearchEvent("error", {"message": str(exc)})
            yield SearchEvent("done", {"itineraries": 0})
            return

        origin = _only_chosen_nodes(origin, query.origin_nodes)
        destination = _only_chosen_nodes(destination, query.destination_nodes)

        yield SearchEvent(
            "resolved",
            {
                "origin": _place_summary(origin),
                "destination": _place_summary(destination),
            },
        )

        extra_hubs = await self._suggest_hubs(origin, destination, query)
        paths = composer.plan(origin, destination, query, resolver, extra_hubs)
        hops = composer.required_hops(paths)

        # Due ondate, avviate insieme. La differenza non e' quando partono ma
        # quanto tempo hanno: gli adapter HTTP chiudono in pochi secondi, quelli
        # che aprono un browser ne hanno bisogno di molti di piu' e i loro
        # risultati si aggiungono alla classifica mentre l'utente gia' legge.
        fast_tasks = composer.tasks_for_tier(paths, query, tier=1)
        slow_tasks = composer.tasks_for_tier(paths, query, tier=2)
        tasks_spec: list[tuple[tuple[str, str], Provider, Any, Any]] = [
            *fast_tasks,  # type: ignore[list-item]
            *slow_tasks,  # type: ignore[list-item]
        ]

        yield SearchEvent(
            "plan",
            {
                "paths": [
                    {"label": path.label, "reason": path.reason} for path in paths
                ],
                "hops": [hop.label for hop in hops.values()],
                "requests": len(tasks_spec),
                "fast_requests": len(fast_tasks),
                "slow_requests": len(slow_tasks),
                "slow_providers": sorted(
                    {provider.id for _, provider, _, _ in slow_tasks}  # type: ignore[misc]
                ),
            },
        )

        # Chi copre la tratta ma non sappiamo interrogare va detto, e va detto
        # subito: e' un'informazione utile anche quando la ricerca poi riesce,
        # perche' quell'operatore potrebbe avere l'orario che serve.
        unreachable = known_routes.suggestions(origin, destination, set(query.modes))
        if unreachable:
            yield SearchEvent("known_routes", unreachable)

        if not tasks_spec:
            yield SearchEvent("error", {"message": _fuori_copertura(origin, destination)})
            yield SearchEvent("done", {"itineraries": 0})
            return

        state = _State()
        queue: asyncio.Queue[SearchEvent | None] = asyncio.Queue()
        ctx = SearchContext(
            date=query.date,
            pax=query.pax,
            with_checked_bag=query.with_checked_bag,
            depart_after=query.depart_after,
            http=get_http_client(),
        )
        semaphore = asyncio.Semaphore(self._settings.search_max_concurrency)
        fast_timeout = max(8.0, min(25.0, self._settings.fast_budget * 0.9))
        slow_timeout = max(20.0, min(70.0, self._settings.search_time_budget * 0.7))

        state.pending_fast = len(fast_tasks)
        state.pending_slow = len(slow_tasks)

        async def worker(
            hop_key: tuple[str, str], provider: Provider, origin_node, dest_node
        ) -> None:
            timeout = fast_timeout if provider.tier <= 1 else slow_timeout
            report = await self._run_provider(
                provider, origin_node, dest_node, ctx, semaphore, timeout,
                state, hop_key,
            )
            if provider.tier <= 1:
                state.pending_fast -= 1
            else:
                state.pending_slow -= 1
            await queue.put(SearchEvent("provider", report))

        workers = [
            asyncio.create_task(worker(hop_key, provider, origin_node, dest_node))
            for hop_key, provider, origin_node, dest_node in tasks_spec
        ]

        async def sentinel() -> None:
            await asyncio.gather(*workers, return_exceptions=True)
            await queue.put(None)

        watcher = asyncio.create_task(sentinel())

        last_refresh = 0.0
        finished = False
        fast_done = not fast_tasks
        started_at = time.monotonic()

        yield SearchEvent(
            "phase",
            {"phase": "veloce", "pending": state.pending_fast, "slow_pending": state.pending_slow},
        )

        try:
            while not finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=min(remaining, 0.5))
                except asyncio.TimeoutError:
                    event = None
                else:
                    if event is None:
                        finished = True
                    else:
                        yield event

                now = time.monotonic()

                # La prima ondata e' chiusa quando i suoi adapter hanno finito,
                # oppure quando il loro budget scade: da qui in poi si aspettano
                # solo i lenti, e va detto.
                if not fast_done and (
                    state.pending_fast <= 0
                    or now - started_at >= self._settings.fast_budget
                ):
                    fast_done = True
                    itineraries = self._build(paths, state, origin, destination, query)
                    state.seen_itineraries.update(item.id for item in itineraries)
                    yield SearchEvent(
                        "phase",
                        {
                            "phase": "approfondita",
                            "pending": state.pending_slow,
                            "providers": sorted(
                                {p.id for _, p, _, _ in slow_tasks}  # type: ignore[misc]
                            ),
                        },
                    )

                should_refresh = finished or (
                    state.total_legs and now - last_refresh >= REFRESH_SECONDS
                )
                if should_refresh:
                    last_refresh = now
                    itineraries = self._build(paths, state, origin, destination, query)
                    if itineraries:
                        yield SearchEvent(
                            "itineraries",
                            {
                                "items": self._flag_new(itineraries[:TOP_N], state, fast_done),
                                "partial": not finished,
                                "phase": "approfondita" if fast_done else "veloce",
                                "relaxed": self._relaxed(itineraries[:TOP_N], state, query),
                            },
                        )
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            watcher.cancel()

        # Chi non ha risposto entro il budget va dichiarato, non nascosto.
        for hop_key, provider, _, _ in tasks_spec:
            if provider.id not in state.reports:
                report = ProviderReport(
                    provider=provider.id,
                    status=ProviderStatus.TIMEOUT,
                    detail="oltre il tempo massimo della ricerca",
                )
                state.reports[provider.id] = report
                yield SearchEvent("provider", report)

        itineraries = self._build(paths, state, origin, destination, query)
        yield SearchEvent(
            "itineraries",
            {
                "items": self._flag_new(itineraries[:TOP_N], state, fast_done),
                "partial": False,
                "phase": "completa",
                "relaxed": self._relaxed(itineraries[:TOP_N], state, query),
            },
        )

        # Zero soluzioni fuori dai paesi coperti non e' «non c'e' niente quel
        # giorno», e' «non sappiamo chiedere a nessuno». Sono due cose diverse e
        # solo una delle due si risolve cambiando data.
        if not itineraries:
            messaggio = _fuori_copertura(origin, destination)
            if "sappiamo interrogare" in messaggio:
                yield SearchEvent("error", {"message": messaggio})

        yield SearchEvent(
            "done",
            {
                "itineraries": len(itineraries),
                "legs": state.total_legs,
                "providers": list(state.reports.values()),
            },
        )

    # ------------------------------------------------------------------ interni

    async def _run_provider(
        self,
        provider: Provider,
        origin_node,
        dest_node,
        ctx: SearchContext,
        semaphore: asyncio.Semaphore,
        timeout: float,
        state: _State,
        hop_key: tuple[str, str],
    ) -> ProviderReport:
        scope = provider.scope(origin_node, dest_node)
        is_open, reason = await circuit_breaker.is_open(provider.id, scope)
        if is_open:
            report = ProviderReport(
                provider=provider.id, status=ProviderStatus.CIRCUIT_OPEN, detail=reason
            )
            state.reports.setdefault(provider.id, report)
            return report

        started = time.monotonic()
        try:
            async with semaphore:
                legs = await asyncio.wait_for(
                    provider.search(origin_node, dest_node, ctx), timeout=timeout
                )
        except NotServed as exc:
            return self._report(state, provider, ProviderStatus.SKIPPED, started, str(exc))
        except asyncio.TimeoutError:
            await circuit_breaker.record_failure(provider.id, "timeout", scope=scope)
            return self._report(
                state, provider, ProviderStatus.TIMEOUT, started, f"oltre {timeout:.0f}s"
            )
        except Blocked as exc:
            await circuit_breaker.record_failure(provider.id, "blocked", str(exc), scope)
            return self._report(state, provider, ProviderStatus.BLOCKED, started, str(exc))
        except ProviderError as exc:
            await circuit_breaker.record_failure(provider.id, "error", str(exc), scope)
            return self._report(state, provider, ProviderStatus.ERROR, started, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - un adapter non deve far cadere la ricerca
            logger.exception("adapter %s in errore inatteso", provider.id)
            await circuit_breaker.record_failure(provider.id, "error", str(exc), scope)
            return self._report(
                state, provider, ProviderStatus.ERROR, started, f"{type(exc).__name__}: {exc}"
            )

        await circuit_breaker.record_success(provider.id, scope)
        if legs:
            state.legs_by_hop[hop_key].extend(legs)
            state.total_legs += len(legs)
        status = ProviderStatus.OK if legs else ProviderStatus.EMPTY
        return self._report(state, provider, status, started, legs=len(legs))

    @staticmethod
    def _report(
        state: _State,
        provider: Provider,
        status: ProviderStatus,
        started: float,
        detail: str | None = None,
        legs: int = 0,
    ) -> ProviderReport:
        elapsed = int((time.monotonic() - started) * 1000)
        existing = state.reports.get(provider.id)
        report = ProviderReport(
            provider=provider.id,
            status=status,
            legs_found=legs + (existing.legs_found if existing else 0),
            elapsed_ms=elapsed,
            detail=detail,
        )
        # Un provider interrogato su piu' tratte: vince l'esito migliore, cosi'
        # non lo si segnala come rotto se ha funzionato almeno una volta.
        if existing is None or _status_rank(status) > _status_rank(existing.status):
            state.reports[provider.id] = report
        else:
            state.reports[provider.id].legs_found = report.legs_found
        return state.reports[provider.id]

    @staticmethod
    def _flag_new(
        itineraries: list[Itinerary], state: _State, fast_done: bool
    ) -> list[Itinerary]:
        """Marca gli itinerari comparsi dopo la prima ondata.

        Prima che la fase veloce sia chiusa e' tutto nuovo e segnalarlo sarebbe
        rumore; dopo, un itinerario che compare all'improvviso ha bisogno di
        dire da dove viene, altrimenti la classifica sembra muoversi da sola."""
        for itinerary in itineraries:
            itinerary.is_new = fast_done and itinerary.id not in state.seen_itineraries
        if fast_done:
            state.seen_itineraries.update(item.id for item in itineraries)
        return itineraries

    def _build(
        self,
        paths: list[composer.CandidatePath],
        state: _State,
        origin: Place,
        destination: Place,
        query: SearchQuery,
    ) -> list[Itinerary]:
        collected: list[Itinerary] = []
        for path in paths:
            collected.extend(
                composer.assemble(path, state.legs_by_hop, origin, destination, query)
            )
        unique = composer.deduplicate(collected)
        kept = ranker.filter_by_query(unique, query)
        # Se i filtri hanno azzerato tutto e' meglio mostrare qualcosa fuori
        # vincolo che una pagina vuota: l'utente decide se gli va bene lo stesso.
        # Ma "decide" presuppone che gli venga detto, ed e' quello che segna
        # questo flag: senza, la pagina mostra orari che l'utente aveva escluso e
        # nulla glielo segnala, la stessa colpa dell'adapter che restituisce zero
        # gambe in silenzio.
        state.constraints_dropped = bool(unique) and not kept
        ranked = ranker.rank(kept or unique, query)
        # Prima si tolgono i doppioni quasi identici, poi si porta in testa il
        # migliore di ogni famiglia: cosi' i primi risultati dicono quali
        # strade esistono, invece di sei varianti della stessa.
        return ranker.diversify(ranker.collapse_similar(ranked))

    def _relaxed(
        self, shown: list[Itinerary], state: _State, query: SearchQuery
    ) -> list[dict[str, object]]:
        """I vincoli da dichiarare, calcolati sulle schede che si stanno spedendo.

        Non su tutto il materiale grezzo: la pagina mostra le prime `TOP_N`, e un
        vincolo violato solo da soluzioni che restano fuori sarebbe un avviso che
        l'utente non puo' verificare guardando lo schermo."""
        if not state.constraints_dropped:
            return []
        return ranker.unmet_constraints(shown, query)

    async def _suggest_hubs(
        self, origin: Place, destination: Place, query: SearchQuery
    ) -> list[str]:
        """Scali suggeriti dall'IA. Silenzioso se non configurata o non risponde."""
        from app.ai.hub_advisor import suggest_hubs

        try:
            return await suggest_hubs(origin, destination, query)
        except Exception:  # noqa: BLE001 - un consiglio mancato non ferma la ricerca
            logger.debug("suggerimento hub non disponibile", exc_info=True)
            return []


_STATUS_RANK = {
    ProviderStatus.OK: 5,
    ProviderStatus.EMPTY: 4,
    ProviderStatus.SKIPPED: 3,
    ProviderStatus.TIMEOUT: 2,
    ProviderStatus.BLOCKED: 1,
    ProviderStatus.CIRCUIT_OPEN: 1,
    ProviderStatus.ERROR: 0,
    ProviderStatus.PENDING: 0,
}


def _status_rank(status: ProviderStatus) -> int:
    return _STATUS_RANK.get(status, 0)


def _only_chosen_nodes(place: Place, chosen: set[str]) -> Place:
    """Restringe la localita' alle fermate scelte a mano, se ce ne sono.

    Vuoto significa "tutte", che e' il comportamento normale: chi scrive
    "Torino" continua a cercare da Porta Nuova, Porta Susa, Stura e
    dall'aeroporto insieme. Le coordinate della localita' non si toccano:
    restano quelle dell'ancora, altrimenti il primo e l'ultimo miglio
    verrebbero misurati dalla fermata invece che dal centro citta' e la
    penalita' per raggiungere una fermata periferica sparirebbe.

    Se il filtro non lascia niente si tiene il posto com'era: un elenco di id
    non piu' validi (una ricerca salvata mesi fa, un dataset aggiornato) non
    deve trasformarsi in "nessuna soluzione"."""
    if not chosen:
        return place
    tenuti = [node for node in place.nodes if node.id in chosen]
    if not tenuti:
        logger.info("fermate scelte non trovate in %s: le ignoro", place.label)
        return place
    return place.model_copy(update={"nodes": tenuti})


def _place_summary(place: Place) -> dict:
    return {
        "label": place.label,
        "lat": place.lat,
        "lon": place.lon,
        "country": place.country,
        "nodes": [
            {"id": node.id, "name": node.name, "kind": node.kind.value}
            for node in place.nodes
        ],
    }
