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


def operator_saving(discount: Discount, leg: Leg, amount: float) -> float | None:
    """Il risparmio che dichiara **l'operatore**, se questa tessera lo sblocca.

    E' l'unico numero che non invecchia: la percentuale scritta da qualche parte
    scade — la Carta Verde e' passata dal 10% al non esistere piu' nel giro di
    una primavera — mentre il prezzo ridotto arriva dentro la risposta di
    ricerca, per quella tratta e per quel giorno.

    Si applica solo se la tessera copre **tutte** le offerte che concorrono al
    totale: se il prezzo basso nasce da due tratte, una scontata con FrecciaYOUNG
    e una con SENIOR, avere solo la prima non lo ottiene. Nel dubbio si applica
    meno, che e' la regola di questo modulo."""
    if not leg.reduced_fares or not discount.offers:
        return None
    ammesse = {_normalize(o) for o in discount.offers}
    richieste = {_normalize(nome) for nome in leg.reduced_fares}
    if not richieste or not richieste <= ammesse:
        return None
    risparmio = round(amount - min(leg.reduced_fares.values()), 2)
    return risparmio if risparmio > 0 else None


def best_for(
    leg: Leg, discounts: list[Discount], amount: float
) -> tuple[Discount, float, bool] | None:
    """La tessera che conviene di piu' su questa tratta, se ce n'e' una.

    Il terzo valore dice se il risparmio viene dall'operatore o e' dichiarato
    dall'utente: sono due cose molto diverse davanti alla cassa, e l'interfaccia
    le scrive in modo diverso.

    Una sola tessera, non tutte: sommarne due sullo stesso biglietto e' quasi
    sempre falso, e nessun operatore lo permette. Se un giorno servira' davvero,
    sara' una decisione esplicita e non un effetto collaterale."""
    if amount <= 0 or not discounts:
        return None

    migliori: list[tuple[Discount, float, bool]] = []
    for discount in discounts:
        if not applies(discount, leg):
            continue
        dall_operatore = operator_saving(discount, leg, amount)
        if dall_operatore is not None:
            migliori.append((discount, dall_operatore, True))
        else:
            migliori.append((discount, discount.saving(amount), False))

    migliori = [voce for voce in migliori if voce[1] > 0]
    if not migliori:
        return None
    # A parita' di risparmio vince quello che viene dall'operatore: e' lo stesso
    # numero, ma uno dei due si puo' difendere.
    return max(migliori, key=lambda voce: (voce[1], voce[2]))
