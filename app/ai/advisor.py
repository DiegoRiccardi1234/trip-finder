"""Il consiglio finale: quale soluzione prendere e perche'.

Una classifica ordinata non e' una decisione. Le prime tre soluzioni sono spesso
incommensurabili fra loro: il volo costa quaranta euro in piu' ma restituisce
una giornata, il pullman notturno risparmia un albergo ma ti scarica alle sei
del mattino, il treno costa di piu' ma e' un biglietto solo. Qui si chiede al
modello di dire il compromesso ad alta voce.

Al modello si passano numeri gia' calcolati, mai dati grezzi: costo totale,
durata porta a porta, numero di biglietti, margine sulla coincidenza piu'
stretta. Non deve fare aritmetica, deve fare da consigliere.
"""

from __future__ import annotations

import logging

from app.ai import client
from app.models import AdviceOption, CompareRequest, Itinerary, SearchQuery
from app.orchestrator import cache

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 6

SYSTEM = """Sei un amico che si intende di viaggi e aiuta a scegliere fra alternative
gia' trovate. Ti vengono dati itinerari con costo totale a persona (bagagli e
trasferimenti inclusi), durata porta a porta, numero di biglietti e avvisi.

Scrivi in italiano, al massimo tre paragrafi brevi:
1. quale prenderesti e per quale motivo concreto;
2. in quale situazione converrebbe invece un'altra opzione;
3. l'avvertenza pratica piu' importante, se ce n'e' una.

Regole:
- Non inventare orari, prezzi o collegamenti che non sono nell'elenco.
- Non ripetere la tabella: chi legge la vede gia'.
- Se un itinerario ha biglietti separati, dillo: e' un rischio reale, perche'
  saltando una coincidenza nessuno riprotegge il passeggero.
- Niente elenchi puntati, niente titoli, niente formule di cortesia."""


def _describe(itinerary: Itinerary, index: int) -> str:
    modes = "+".join(mode.value for mode in itinerary.modes)
    operators = ", ".join(itinerary.operators)
    parts = [
        f"{index}. {itinerary.depart:%d/%m %H:%M} - {itinerary.arrive:%H:%M}"
        f" ({itinerary.duration_min // 60}h{itinerary.duration_min % 60:02})",
        f"{itinerary.cost.total:.2f} euro a persona",
        f"mezzi {modes} ({operators})",
        f"{itinerary.n_changes} cambi, {itinerary.n_tickets} biglietti",
    ]
    if itinerary.min_connection_min is not None:
        parts.append(f"coincidenza piu' stretta {itinerary.min_connection_min} min")
    if itinerary.overnight:
        parts.append("si viaggia di notte")
    if itinerary.cost.has_estimates:
        parts.append("alcune voci di costo sono stimate")
    if itinerary.flags:
        parts.append("avvisi: " + ", ".join(flag.value for flag in itinerary.flags))
    return " | ".join(parts)


async def advise(itineraries: list[Itinerary], query: SearchQuery) -> str | None:
    if not client.is_configured() or not itineraries:
        return None

    candidates = itineraries[:MAX_CANDIDATES]
    context = [
        f"Viaggio: {query.origin} -> {query.destination} il {query.date.isoformat()}",
        f"Passeggeri: {query.pax}",
        f"Valigia da stiva: {'si' if query.with_checked_bag else 'no'}",
    ]
    if query.max_budget:
        context.append(f"Budget massimo: {query.max_budget:.0f} euro")
    if query.depart_after:
        context.append(f"Non puo' partire prima delle {query.depart_after:%H:%M}")
    if query.arrive_by:
        context.append(f"Deve arrivare entro le {query.arrive_by:%H:%M}")
    if not query.allow_night:
        context.append("Non vuole viaggiare di notte")
    if query.raw_text:
        context.append(f"Richiesta originale: {query.raw_text}")

    prompt = "\n".join(context) + "\n\nItinerari:\n" + "\n".join(
        _describe(itinerary, index) for index, itinerary in enumerate(candidates, start=1)
    )

    completion = await client.complete(
        "advice", SYSTEM, prompt, max_tokens=600, temperature=0.4
    )
    if completion is None:
        return None
    logger.debug("consiglio prodotto da %s", completion.model)
    return completion.text


# --- Confronto fra piu' ricerche -------------------------------------------

COMPARE_SYSTEM = """Sei un amico che si intende di viaggi. Chi ti scrive non ha ancora
deciso **dove** (o quando, o da dove) andare: ti mostra piu' possibilita', ognuna
con le sue prime soluzioni di viaggio gia' trovate, e vuole sapere quale conviene.

Scrivi in italiano, al massimo tre paragrafi brevi:
1. quale possibilita' sceglieresti, dicendo di quanto e in cosa batte le altre
   (euro di differenza, ore di differenza, cambi in meno);
2. a quali condizioni converrebbe invece un'altra;
3. l'avvertenza pratica piu' importante, se ce n'e' una.

Regole:
- Confronta le possibilita' fra loro. Non descriverle una per una: chi legge le
  ha gia' sotto gli occhi, quello che gli manca e' il paragone.
- Chiama ogni possibilita' con la sua etichetta, cosi' come te la do.
- Non inventare orari, prezzi o collegamenti che non sono nell'elenco.
- Se una possibilita' obbliga a biglietti separati, dillo: saltando una
  coincidenza nessuno riprotegge il passeggero.
- Niente elenchi puntati, niente titoli, niente formule di cortesia."""


def _describe_option(option: AdviceOption, index: int) -> str:
    parts = [
        f"{index}. {option.depart:%d/%m %H:%M} - {option.arrive:%H:%M}"
        f" ({option.duration_min // 60}h{option.duration_min % 60:02})",
    ]
    if option.return_depart and option.return_arrive:
        parts.append(
            f"ritorno {option.return_depart:%d/%m %H:%M} - {option.return_arrive:%H:%M}"
        )
    parts += [
        f"{option.total:.2f} euro a persona in tutto",
        f"mezzi {'+'.join(option.modes)} ({', '.join(option.operators)})",
        f"{option.n_changes} cambi, {option.n_tickets} biglietti",
    ]
    if option.flags:
        parts.append("avvisi: " + ", ".join(option.flags))
    return " | ".join(parts)


async def compare(request: CompareRequest) -> str | None:
    """Un consiglio solo su piu' possibilita', per dire quale prendere.

    Non e' `advise` chiamata piu' volte: quella descrive le righe di **una**
    classifica e le sue righe sono anonime rispetto alla meta, quindi due
    consigli affiancati non possono dire "vai a Palermo, costa 30 euro meno".
    Qui il confronto e' il compito, e ogni possibilita' arriva al modello con
    la sua etichetta."""
    if not client.is_configured():
        return None
    utili = [candidate for candidate in request.candidates if candidate.options]
    if len(utili) < 2:
        return None

    context = [f"Passeggeri: {request.pax}"]
    if request.with_checked_bag:
        context.append("Con valigia da stiva")
    if request.max_budget:
        context.append(f"Budget massimo: {request.max_budget:.0f} euro")
    if request.raw_text:
        context.append(f"Richiesta originale: {request.raw_text}")

    blocchi = []
    for candidate in utili:
        testa = f"{candidate.label} ({candidate.origin} -> {candidate.destination}, {candidate.date:%d/%m}"
        testa += f", ritorno il {candidate.return_date:%d/%m})" if candidate.return_date else ")"
        righe = "\n".join(
            _describe_option(option, index)
            for index, option in enumerate(candidate.options, start=1)
        )
        blocchi.append(f"{testa}\n{righe}")

    prompt = "\n".join(context) + "\n\nPossibilita':\n\n" + "\n\n".join(blocchi)

    # Stessa chiave a parita' di possibilita' confrontate: rifare la stessa
    # ricerca due volte di fila non deve ricomprare un consiglio identico.
    key = cache.make_key(
        "ai:compare",
        request.pax,
        request.max_budget,
        sorted(
            f"{c.label}|{c.date}|{c.return_date}|"
            + ";".join(f"{o.depart:%H%M}/{o.total:.2f}" for o in c.options)
            for c in utili
        ),
    )
    completion = await client.complete(
        "advice", COMPARE_SYSTEM, prompt, max_tokens=700, temperature=0.4, cache_key=key
    )
    if completion is None:
        return None
    logger.debug("confronto prodotto da %s", completion.model)
    return completion.text
