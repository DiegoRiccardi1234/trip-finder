"""Costo reale porta-a-porta di un itinerario.

Il confronto fra un volo e un treno fatto sulle sole tariffe e' sbagliato quasi
sempre. Il volo aggiunge il bagaglio, la navetta per l'aeroporto e il
trasferimento all'arrivo; il treno di solito no. Questo modulo mette tutto nella
stessa colonna e dichiara quali voci sono stimate, cosi' l'utente sa dove il
numero e' solido e dove no.
"""

from __future__ import annotations

from app.models import CostBreakdown, CostLine, Leg, Mode, SearchQuery
from app.routing import discounts, transfers

#: Prezzo di un bagaglio da stiva a tratta, per operatore.
#: Le compagnie che lo includono valgono zero e non vanno indovinate.
CHECKED_BAG_PRICE: dict[str, float] = {
    "ryanair": 45.0,
    "wizzair": 44.0,
    "easyjet": 42.0,
    "vueling": 40.0,
    "volotea": 40.0,
    "transavia": 40.0,
    "ita": 0.0,  # incluso su tutte le tariffe tranne la Light
    "lufthansa": 0.0,
    "airfrance": 0.0,
    "trenitalia": 0.0,
    "italo": 0.0,
    "flixbus": 0.0,
    "itabus": 0.0,
    "marino": 0.0,
    "marozzi": 0.0,
    "fal": 0.0,
}
#: Compagnie aeree sconosciute: si assume il modello low cost, che e' il caso
#: peggiore e quello statisticamente piu' probabile sulle rotte europee.
UNKNOWN_AIR_BAG_PRICE = 45.0

#: Bagaglio a mano grande, dove non e' incluso nella tariffa base.
CABIN_BAG_PRICE: dict[str, float] = {
    "ryanair": 25.0,
    "wizzair": 25.0,
    "easyjet": 25.0,
}


#: Costo indicativo per chilometro quando la tariffa non e' pubblicata. Sono
#: ordini di grandezza presi dai listini italiani, non prezzi veri: servono solo
#: a evitare che una tratta senza prezzo risulti gratis.
FARE_PER_KM: dict[Mode, float] = {
    Mode.RAIL: 0.10,
    Mode.BUS: 0.07,
    Mode.FERRY: 0.25,
    Mode.AIR: 0.15,
}
MIN_ESTIMATED_FARE = 2.5


def estimate_fare(leg: Leg) -> float:
    """Tariffa indicativa di una tratta di cui l'operatore non pubblica il prezzo."""
    from app.models import node_distance_km

    distance = node_distance_km(leg.origin, leg.destination) * 1.25
    rate = FARE_PER_KM.get(leg.mode, 0.10)
    return round(max(MIN_ESTIMATED_FARE, distance * rate), 2)


def checked_bag_price(leg: Leg) -> float:
    if leg.mode is not Mode.AIR:
        return CHECKED_BAG_PRICE.get(leg.provider, 0.0)
    if leg.fare and leg.fare.included_checked_bags > 0:
        return 0.0
    if leg.fare and leg.fare.checked_bag_price is not None:
        return leg.fare.checked_bag_price
    return CHECKED_BAG_PRICE.get(leg.provider, UNKNOWN_AIR_BAG_PRICE)


def _discount_line(leg: Leg, query: SearchQuery, amount: float) -> CostLine | None:
    """La riga di sconto per questa tratta, se una tessera dell'utente ci vale.

    Va detto sempre quando lo sconto e' **dichiarato** e non letto
    dall'operatore: e' una cifra che l'utente ha scritto lui, e alla cassa puo'
    smentirla."""
    scelto = discounts.best_for(leg, query.discounts, amount)
    if scelto is None:
        return None
    tessera, risparmio = scelto
    etichetta = tessera.name if tessera.verified else f"{tessera.name} (dichiarato da te)"
    return CostLine(label=etichetta, amount=-risparmio, kind="discount")


def compute(legs: list[Leg], query: SearchQuery) -> CostBreakdown:
    """Somma tariffe, bagagli e trasferimenti in un'unica cifra per persona."""
    lines: list[CostLine] = []

    for leg in legs:
        if leg.is_transfer:
            estimate = transfers.estimate(leg.origin, leg.destination)
            if estimate.price_eur > 0:
                prezzo = round(estimate.price_eur, 2)
                lines.append(
                    CostLine(
                        label=f"{leg.origin.name} - {leg.destination.name}",
                        amount=prezzo,
                        kind="transfer",
                        estimated=estimate.estimated,
                    )
                )
                sconto = _discount_line(leg, query, prezzo)
                if sconto is not None:
                    lines.append(sconto)
            continue

        label = f"{leg.operator or leg.provider}: {leg.origin.name} - {leg.destination.name}"
        if leg.fare is not None:
            prezzo = round(leg.fare.amount, 2)
            lines.append(CostLine(label=label, amount=prezzo, kind="fare"))
            sconto = _discount_line(leg, query, prezzo)
            if sconto is not None:
                lines.append(sconto)
        else:
            # Una gamba senza prezzo non vale zero. Metterla a zero premierebbe
            # proprio gli operatori su cui sappiamo meno: FAL non pubblica le
            # tariffe, e un itinerario che la usa risulterebbe il piu' economico
            # di tutti senza alcun motivo. Si stima dalla distanza, con la voce
            # dichiarata come stima.
            stimato = estimate_fare(leg)
            lines.append(
                CostLine(
                    label=f"{label} (prezzo stimato, non pubblicato)",
                    amount=stimato,
                    kind="fare",
                    estimated=True,
                )
            )
            sconto = _discount_line(leg, query, stimato)
            if sconto is not None:
                lines.append(sconto)

        if query.with_checked_bag:
            price = checked_bag_price(leg)
            if price > 0:
                lines.append(
                    CostLine(
                        label=f"bagaglio in stiva - {leg.operator or leg.provider}",
                        amount=price,
                        kind="bag",
                        estimated=leg.provider not in CHECKED_BAG_PRICE,
                    )
                )

    return CostBreakdown(currency="EUR", lines=lines)


def has_unknown_fare(legs: list[Leg]) -> bool:
    return any(leg.fare is None for leg in legs if not leg.is_transfer)
