"""Quali tessere valgono su quale tratta.

Il calcolo dello sconto e' banale (`Discount.saving`); la parte che sbaglia e'
decidere **se** si applica. Qui stanno quelle regole, in un posto solo, perche'
un'applicazione troppo generosa non da' errore: da' un prezzo piu' basso di
quello che l'utente paghera' davvero, che e' esattamente la classe di bug che
ha gia' colpito questo progetto due volte (Trenitalia esaurito, Itabus con
cambio).

Nel dubbio si applica **meno**: uno sconto mancato si vede alla cassa e fa
piacere, uno sconto inventato fa perdere il viaggio.
"""

from __future__ import annotations

import unicodedata

from app.models import Discount, Leg


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return " ".join(cleaned.split())


def _place_names(leg: Leg) -> tuple[set[str], set[str]]:
    """Come si puo' chiamare ciascun capo della tratta."""
    def nomi(node) -> set[str]:
        out = {_normalize(node.name)}
        if node.city:
            out.add(_normalize(node.city))
        # "Torino Porta Nuova" deve corrispondere anche a "Torino".
        out |= {" ".join(_normalize(node.name).split()[:1])}
        return {n for n in out if n}

    return nomi(leg.origin), nomi(leg.destination)


def _scope_matches(discount: Discount, leg: Leg) -> bool:
    scope = (discount.scope or "all").strip().lower()
    if scope in ("", "all"):
        # Uno sconto "su tutto" non tocca i trasferimenti: un abbonamento
        # urbano si dichiara per quello, e applicarlo per default renderebbe
        # gratis anche il taxi per l'aeroporto.
        return not leg.is_transfer
    if scope.startswith("provider:"):
        return leg.provider == scope.split(":", 1)[1]
    if scope.startswith("mode:"):
        wanted = scope.split(":", 1)[1]
        if wanted == "transfer":
            return leg.is_transfer
        return leg.mode.value == wanted
    return False


def _route_matches(discount: Discount, leg: Leg) -> bool:
    if not discount.routes:
        return True
    partenza, arrivo = _place_names(leg)
    for route in discount.routes:
        pezzi = [_normalize(p) for p in str(route).replace("→", "-").split("-")]
        pezzi = [p for p in pezzi if p]
        if len(pezzi) != 2:
            continue
        a, b = pezzi
        # Vale nei due versi: chi ha l'abbonamento Torino-Matera torna indietro
        # con lo stesso.
        if (a in partenza and b in arrivo) or (b in partenza and a in arrivo):
            return True
    return False


def _when_matches(discount: Discount, leg: Leg) -> bool:
    giorno = leg.depart.date()
    if discount.valid_from and giorno < discount.valid_from:
        return False
    if discount.valid_to and giorno > discount.valid_to:
        return False
    if discount.weekdays and giorno.weekday() not in discount.weekdays:
        return False
    return True


def applies(discount: Discount, leg: Leg) -> bool:
    if not discount.active:
        return False
    return (
        _scope_matches(discount, leg)
        and _route_matches(discount, leg)
        and _when_matches(discount, leg)
    )


def best_for(leg: Leg, discounts: list[Discount], amount: float) -> tuple[Discount, float] | None:
    """La tessera che conviene di piu' su questa tratta, se ce n'e' una.

    Una sola, non tutte: sommare due tessere sullo stesso biglietto e' quasi
    sempre falso, e nessun operatore lo permette. Se un giorno servira'
    davvero, sara' una decisione esplicita e non un effetto collaterale."""
    if amount <= 0 or not discounts:
        return None
    migliori = [
        (discount, discount.saving(amount))
        for discount in discounts
        if applies(discount, leg)
    ]
    migliori = [(d, s) for d, s in migliori if s > 0]
    if not migliori:
        return None
    return max(migliori, key=lambda pair: pair[1])
