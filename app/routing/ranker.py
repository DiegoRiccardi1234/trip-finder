"""Classifica gli itinerari secondo i criteri che contano davvero.

Nessun singolo numero descrive un viaggio. Il piu' economico puo' costare un
giorno di ferie; il piu' veloce puo' avere una coincidenza di venti minuti fra
due biglietti separati; il notturno "dura" quattordici ore ma ti fa risparmiare
un albergo. Qui ogni criterio viene normalizzato sull'insieme dei candidati e
pesato, e il punteggio resta scomponibile perche' l'utente possa vedere perche'
una soluzione ha vinto.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from app.models import Itinerary, RiskFlag, SearchQuery
from app.routing import feasibility


def _normalize(values: list[float]) -> list[float]:
    """Porta i valori in [0, 1]. Se sono tutti uguali, valgono tutti zero."""
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [0.0] * len(values)
    return [(value - low) / (high - low) for value in values]


def risk_score(itinerary: Itinerary) -> float:
    """Da 0 (nessun rischio) a 1 (molto probabile che qualcosa vada storto)."""
    score = 0.0
    bookable = [leg for leg in itinerary.legs if not leg.is_transfer]

    if itinerary.n_tickets > 1:
        # Il salto vero e' fra uno e due biglietti: da li' in poi si aggrava,
        # ma la differenza qualitativa e' gia' avvenuta.
        score += 0.35 + 0.12 * (itinerary.n_tickets - 2)

    margin = itinerary.min_connection_min
    if margin is not None and len(bookable) > 1:
        required = max(
            feasibility.required_margin(prev, nxt)
            for prev, nxt in zip(bookable, bookable[1:])
        )
        ratio = margin / max(1, required)
        if ratio < 1.0:
            score += 0.45
        elif ratio < 1.5:
            score += 0.28
        elif ratio < 2.5:
            score += 0.12

    # I cambi interni a un biglietto unico sono protetti, ma restano occasioni
    # per un ritardo: pesano poco, non zero.
    score += 0.04 * sum(leg.internal_changes for leg in bookable)

    return min(1.0, score)


def arrival_penalty(itinerary: Itinerary) -> float:
    """Arrivare nel cuore della notte costa un taxi o una notte di albergo."""
    hour = itinerary.arrive.hour
    if 0 <= hour < 5:
        return 1.0
    if 5 <= hour < 6:
        return 0.6
    if hour >= 23:
        return 0.4
    return 0.0


def night_bonus(itinerary: Itinerary) -> float:
    """Un notturno lungo sostituisce un pernottamento: e' un vantaggio."""
    if not itinerary.overnight:
        return 0.0
    if itinerary.duration_min >= 8 * 60:
        return 1.0
    return 0.5


def compute_flags(itinerary: Itinerary) -> list[RiskFlag]:
    flags: list[RiskFlag] = []
    bookable = [leg for leg in itinerary.legs if not leg.is_transfer]

    if itinerary.n_tickets > 1:
        flags.append(RiskFlag.SEPARATE_TICKETS)

    margin = itinerary.min_connection_min
    if margin is not None and len(bookable) > 1:
        required = max(
            feasibility.required_margin(prev, nxt)
            for prev, nxt in zip(bookable, bookable[1:])
        )
        if margin < required * 1.5:
            flags.append(RiskFlag.TIGHT_CONNECTION)

    for prev, nxt in zip(bookable, bookable[1:]):
        if prev.destination.id != nxt.origin.id:
            flags.append(RiskFlag.STATION_CHANGE)
            break

    if 0 <= itinerary.arrive.hour < 6:
        flags.append(RiskFlag.NIGHT_ARRIVAL)

    if itinerary.cost.has_estimates:
        flags.append(RiskFlag.ESTIMATED_COST)

    return flags


def rank(itineraries: list[Itinerary], query: SearchQuery) -> list[Itinerary]:
    """Assegna punteggio e ordina. Modifica gli itinerari in posto."""
    if not itineraries:
        return []

    weights = query.weights
    prices = _normalize([item.cost.total for item in itineraries])
    durations = _normalize([float(item.duration_min) for item in itineraries])
    co2_values = [item.co2_kg or 0.0 for item in itineraries]
    co2 = _normalize(co2_values) if any(co2_values) else [0.0] * len(itineraries)

    for index, itinerary in enumerate(itineraries):
        risk = risk_score(itinerary)
        night = night_bonus(itinerary)
        late = arrival_penalty(itinerary)

        parts = {
            # Le componenti sono "quanto e' buono", non "quanto e' alto":
            # cosi' il punteggio totale si legge sempre come piu' alto = meglio.
            "prezzo": weights.price * (1.0 - prices[index]),
            "durata": weights.duration * (1.0 - durations[index]),
            "affidabilita": weights.risk * (1.0 - risk),
            "notturno": weights.night_bonus * night,
            "ora_arrivo": weights.arrival_penalty * (1.0 - late),
            "co2": weights.co2 * (1.0 - co2[index]),
        }
        itinerary.score_parts = {k: round(v, 3) for k, v in parts.items()}
        itinerary.score = round(sum(parts.values()), 3)
        itinerary.flags = compute_flags(itinerary)

    itineraries.sort(key=lambda item: item.score, reverse=True)
    return itineraries


#: Due viaggi con gli stessi operatori, stesso arrivo e partenza ravvicinata
#: sono lo stesso viaggio preso da due fermate diverse della stessa citta'.
SIMILAR_ARRIVAL_MINUTES = 15
SIMILAR_DEPARTURE_MINUTES = 45


def _similar(a: Itinerary, b: Itinerary) -> bool:
    if a.operators != b.operators:
        return False
    arrival_gap = abs((a.arrive - b.arrive).total_seconds()) / 60
    departure_gap = abs((a.depart - b.depart).total_seconds()) / 60
    return (
        arrival_gap <= SIMILAR_ARRIVAL_MINUTES
        and departure_gap <= SIMILAR_DEPARTURE_MINUTES
    )


def collapse_similar(itineraries: list[Itinerary]) -> list[Itinerary]:
    """Toglie i doppioni quasi identici, tenendo il migliore.

    Il pullman delle 18:30 da corso Vittorio Emanuele e quello delle 18:08 da
    corso Giulio Cesare sono la stessa corsa presa a due fermate di distanza:
    occupano due posti in classifica per dire la stessa cosa."""
    kept: list[Itinerary] = []
    for itinerary in itineraries:
        if any(_similar(itinerary, other) for other in kept):
            continue
        kept.append(itinerary)
    return kept


def diversify(itineraries: list[Itinerary]) -> list[Itinerary]:
    """Porta in testa il migliore di ogni famiglia di soluzione.

    Senza questo, una classifica per solo punteggio mostra sei varianti di
    pullman e nasconde il volo, il treno e il traghetto piu' in basso. Chi
    cerca vuole prima capire **quali strade esistono**, e poi scegliere dentro
    quella che gli interessa."""
    if not itineraries:
        return []

    best_by_family: dict[tuple[str, ...], Itinerary] = {}
    for itinerary in itineraries:  # gia' ordinati per punteggio
        family = tuple(mode.value for mode in itinerary.modes)
        best_by_family.setdefault(family, itinerary)

    # I primati vanno in testa anche se la loro famiglia e' gia' rappresentata:
    # il pullman piu' economico non deve sparire solo perche' un altro pullman
    # ha un punteggio complessivo migliore. Chi cerca vuole vedere il meno caro
    # e il piu' rapido, sempre.
    standouts = [
        min(itineraries, key=lambda item: item.cost.total),
        min(itineraries, key=lambda item: item.duration_min),
    ]

    head: list[Itinerary] = []
    chosen: set[int] = set()
    for itinerary in [*best_by_family.values(), *standouts]:
        if id(itinerary) not in chosen:
            chosen.add(id(itinerary))
            head.append(itinerary)

    head.sort(key=lambda item: item.score, reverse=True)
    return head + [item for item in itineraries if id(item) not in chosen]


def _arrives_too_late(itinerary: Itinerary, query: SearchQuery) -> bool:
    """Vero se l'arrivo sfora l'ora chiesta.

    Un arrivo del giorno dopo sfora sempre: chi scrive "entro le 23:59" non
    intende la notte seguente. Sta in una funzione sola perche' la usano sia il
    filtro sia il rendiconto dei vincoli non rispettati, e se divergessero
    l'utente si sentirebbe dire che un vincolo e' stato messo da parte mentre
    invece era attivo, o il contrario."""
    if not query.arrive_by:
        return False
    if itinerary.arrive.date() > query.date:
        return True
    return itinerary.arrive.date() == query.date and itinerary.arrive.time() > query.arrive_by


def _departs_too_early(itinerary: Itinerary, query: SearchQuery) -> bool:
    """Vero se la partenza precede l'ora chiesta, **nel giorno chiesto**.

    Simmetrica a `_arrives_too_late`, e per lo stesso motivo: "dopo le 13:30" e'
    un'ora di quel giorno, non di un giorno qualunque. Confrontando le sole ore,
    un viaggio che parte alle 23:50 della sera prima passava il vincolo a pieni
    voti — non e' tardi abbastanza, e' presto di un giorno — e a chi cercava il
    ritorno dopo un impegno veniva proposta la notte **precedente** l'impegno.

    Una partenza posticipata al giorno dopo non viola niente: chi mette un'ora
    minima sta dicendo da quando e' libero, non entro quando vuole essere
    partito. A dire l'ultima parola su quello e' `arrive_by`."""
    if not query.depart_after:
        return False
    if itinerary.depart.date() < query.date:
        return True
    return itinerary.depart.date() == query.date and itinerary.depart.time() < query.depart_after


def _travels_at_night(itinerary: Itinerary) -> bool:
    """Movimento fra 01:00 e 05:00, anche breve o di avvicinamento.

    `overnight` identifica le tratte lunghe che sostituiscono un pernottamento:
    esclude trasferimenti e gambe sotto quattro ore. Il vincolo dell'utente
    vale invece per tutto il viaggio porta a porta. Filtro e rendiconto devono
    condividere questa regola, senza modificare il bonus dei notturni lunghi.
    """
    for leg in itinerary.legs:
        depart = leg.depart
        arrive = leg.arrive
        if leg.depart.tzinfo is not None:
            depart = depart.astimezone(timezone.utc)
            arrive = arrive.astimezone(timezone.utc)
        if arrive <= depart:
            continue
        # Il calendario resta locale, i confronti usano gli istanti reali:
        # nel cambio autunnale 02:45 fold=0 precede 02:15 fold=1.
        local_arrive = leg.arrive
        if leg.depart.tzinfo is not None:
            local_arrive = local_arrive.astimezone(leg.depart.tzinfo)
        day = leg.depart.date()
        while day <= local_arrive.date():
            night_start = datetime.combine(day, time(1), tzinfo=leg.depart.tzinfo)
            night_end = datetime.combine(day, time(5), tzinfo=leg.depart.tzinfo)
            if leg.depart.tzinfo is not None:
                night_start = night_start.astimezone(timezone.utc)
                night_end = night_end.astimezone(timezone.utc)
            if depart < night_end and arrive > night_start:
                return True
            day += timedelta(days=1)
    return False


def filter_by_query(itineraries: list[Itinerary], query: SearchQuery) -> list[Itinerary]:
    """Applica i vincoli espliciti dell'utente. Restano fuori solo le violazioni."""
    kept: list[Itinerary] = []
    for itinerary in itineraries:
        if query.max_budget is not None and itinerary.cost.total > query.max_budget:
            continue
        if query.max_changes is not None and itinerary.n_changes > query.max_changes:
            continue
        if not query.allow_night and _travels_at_night(itinerary):
            continue
        if _departs_too_early(itinerary, query):
            continue
        if _arrives_too_late(itinerary, query):
            continue
        kept.append(itinerary)
    return kept


def unmet_constraints(
    itineraries: list[Itinerary], query: SearchQuery
) -> list[dict[str, object]]:
    """I vincoli dell'utente che qualche itinerario di questa lista viola.

    Serve a chi, non avendo trovato niente dentro i vincoli, decide di mostrare
    le soluzioni fuori vincolo invece di una pagina vuota: dire **quali** vincoli
    sta mettendo da parte e' la differenza fra una scelta offerta all'utente e un
    risultato che lo inganna, perche' altrimenti legge orari che aveva chiesto di
    non vedere senza un modo di accorgersene.

    Si nominano solo i vincoli che escludono davvero qualcosa: un tetto di tre
    cambi che nessuno supera non c'entra niente col perche' la lista era vuota, e
    citarlo sposterebbe l'attenzione dal vincolo che invece morde.

    La frase per l'utente la scrive l'interfaccia: qui si dice quale vincolo e
    con che valore."""
    unmet: list[dict[str, object]] = []
    if query.max_budget is not None and any(
        item.cost.total > query.max_budget for item in itineraries
    ):
        unmet.append({"kind": "max_budget", "value": round(query.max_budget, 2)})
    if query.max_changes is not None and any(
        item.n_changes > query.max_changes for item in itineraries
    ):
        unmet.append({"kind": "max_changes", "value": query.max_changes})
    if not query.allow_night and any(_travels_at_night(item) for item in itineraries):
        unmet.append({"kind": "allow_night", "value": False})
    if query.depart_after and any(
        _departs_too_early(item, query) for item in itineraries
    ):
        unmet.append(
            {"kind": "depart_after", "value": query.depart_after.strftime("%H:%M")}
        )
    if query.arrive_by and any(_arrives_too_late(item, query) for item in itineraries):
        unmet.append({"kind": "arrive_by", "value": query.arrive_by.strftime("%H:%M")})
    return unmet
