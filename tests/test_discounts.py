"""Le tessere dell'utente dentro il costo porta-a-porta.

Uno sconto applicato dove non vale non da' errore: da' un prezzo piu' basso di
quello che si paghera' alla cassa, e siccome il ranking ordina sul totale,
quella soluzione si prende anche la prima riga. E' la stessa forma dei due bug
gia' visti (Trenitalia esaurito, Itabus con cambio), per questo qui si verifica
soprattutto **quando lo sconto non si applica**.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models import Discount, DiscountKind, Mode, SearchQuery
from app.routing import cost, discounts

from .conftest import BARI_C, MATERA_BUS, TORINO_PN, at, make_leg


@pytest.fixture
def tratta():
    return make_leg("marino", Mode.BUS, TORINO_PN, MATERA_BUS, at(18, 30), at(8, 45, 1), 91.0)


def _query(*tessere: Discount) -> SearchQuery:
    return SearchQuery(
        origin="Torino", destination="Matera", date=date(2026, 8, 14), discounts=list(tessere)
    )


def test_una_percentuale_abbassa_il_totale_e_si_vede_da_dove(tratta) -> None:
    tessera = Discount(
        name="Tessera universitaria", scope="provider:marino",
        kind=DiscountKind.PERCENT, value=20,
    )
    breakdown = cost.compute([tratta], _query(tessera))

    assert breakdown.total == 91.0 - 18.2
    riga = [line for line in breakdown.lines if line.kind == "discount"]
    assert len(riga) == 1
    assert riga[0].amount == -18.2
    # Dichiarato dall'utente, non letto dall'operatore: va detto nell'etichetta.
    assert "dichiarato da te" in riga[0].label


def test_lo_sconto_di_un_operatore_non_vale_per_un_altro() -> None:
    tessera = Discount(name="Tessera universitaria", scope="provider:marino", value=20)
    altra = make_leg("itabus", Mode.BUS, TORINO_PN, MATERA_BUS, at(9), at(20), 60.0)

    breakdown = cost.compute([altra], _query(tessera))
    assert breakdown.total == 60.0
    assert not [line for line in breakdown.lines if line.kind == "discount"]


def test_le_due_tessere_non_si_sommano(tratta) -> None:
    """Nessun operatore cumula due sconti sullo stesso biglietto, e sommarli
    qui vorrebbe dire mostrare un prezzo che non esiste."""
    breakdown = cost.compute(
        [tratta],
        _query(
            Discount(name="Universitaria", scope="provider:marino", value=20),
            Discount(name="Under 26", scope="mode:bus", value=10),
        ),
    )
    righe = [line for line in breakdown.lines if line.kind == "discount"]
    assert len(righe) == 1
    assert righe[0].amount == -18.2  # vince la piu' conveniente, non la somma


def test_i_vincoli_di_tratta_giorno_e_periodo_valgono(tratta) -> None:
    base = dict(name="Abbonamento", scope="provider:marino", kind=DiscountKind.FREE)

    # La tratta e' Torino-Matera: un abbonamento Bari-Matera non c'entra.
    altrove = Discount(**base, routes=["Bari-Matera"])
    assert not discounts.applies(altrove, tratta)
    # Lo stesso abbonamento sulla tratta giusta vale, e nei due versi.
    assert discounts.applies(Discount(**base, routes=["Matera-Torino"]), tratta)

    # 14 agosto 2026 e' un venerdi' (4). Un feriale-lunedi' non copre.
    assert not discounts.applies(Discount(**base, weekdays=[0]), tratta)
    assert discounts.applies(Discount(**base, weekdays=[4]), tratta)

    assert not discounts.applies(Discount(**base, valid_to=date(2026, 7, 31)), tratta)
    assert not discounts.applies(Discount(**base, valid_from=date(2026, 9, 1)), tratta)
    assert not discounts.applies(Discount(**base, active=False), tratta)


def test_un_abbonamento_azzera_la_tratta_ma_non_il_resto(tratta) -> None:
    tessera = Discount(name="Abbonamento", scope="provider:marino", kind=DiscountKind.FREE)
    query = _query(tessera)
    query.with_checked_bag = True

    breakdown = cost.compute([tratta], query)
    assert breakdown.subtotal("fare") + breakdown.subtotal("discount") == 0.0
    assert breakdown.total == 0.0  # Marino non fa pagare la stiva


def test_uno_sconto_su_tutto_non_regala_i_trasferimenti() -> None:
    """Il taxi per l'aeroporto non e' scontato dalla tessera del treno.

    Va dichiarato apposta (`mode:transfer`), altrimenti una tessera "su tutto"
    finirebbe per abbassare anche voci che con quell'operatore non c'entrano."""
    trasferimento = make_leg(
        "transfer", Mode.TRANSFER, TORINO_PN, BARI_C, at(6), at(7)
    )
    generica = Discount(name="Sconto generico", scope="all", value=50)
    assert not discounts.applies(generica, trasferimento)

    urbano = Discount(name="Abbonamento urbano", scope="mode:transfer", kind=DiscountKind.FREE)
    assert discounts.applies(urbano, trasferimento)


def test_lo_sconto_non_porta_mai_un_prezzo_sotto_zero(tratta) -> None:
    esagerata = Discount(name="Buono", scope="all", kind=DiscountKind.AMOUNT, value=500)
    breakdown = cost.compute([tratta], _query(esagerata))
    assert breakdown.total == 0.0


# --- la tariffa che manda l'operatore ---------------------------------------
#
# La percentuale scritta da qualche parte scade: la Carta Verde e' passata dal
# 10% al non esistere piu' nel giro di una primavera. Il prezzo ridotto che
# Trenitalia manda dentro la risposta di ricerca no, perche' vale per quella
# corsa e per quel giorno. Quando c'e', vince.


def _treno_con_tariffe_ridotte(**ridotte: float):
    treno = make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(9), at(18), 100.0)
    treno.reduced_fares = dict(ridotte)
    return treno


def test_la_tariffa_dell_operatore_batte_la_percentuale_dichiarata() -> None:
    treno = _treno_con_tariffe_ridotte(FrecciaYOUNG=61.0)
    tessera = Discount(
        name="Promo Young", scope="provider:trenitalia", value=20, offers=["FrecciaYOUNG"]
    )
    breakdown = cost.compute([treno], _query(tessera))

    # 39 euro dall'operatore, non i 20 che darebbe la percentuale.
    assert breakdown.total == 61.0
    riga = next(line for line in breakdown.lines if line.kind == "discount")
    assert riga.amount == -39.0
    assert "tariffa dell'operatore" in riga.label
    assert "dichiarato da te" not in riga.label


def test_senza_la_tessera_giusta_la_tariffa_ridotta_non_si_applica() -> None:
    """Il prezzo c'e' nella risposta, ma e' di chi ha diritto a quell'offerta.

    Applicarlo a chiunque vorrebbe dire mostrare un prezzo che alla cassa non
    esiste, che e' il difetto piu' caro di questo progetto."""
    treno = _treno_con_tariffe_ridotte(FrecciaYOUNG=61.0)
    senior = Discount(
        name="Promo Senior", scope="provider:trenitalia", value=20, offers=["SENIOR"]
    )
    breakdown = cost.compute([treno], _query(senior))

    # La sua percentuale vale lo stesso, ma dichiarata: sono due cose diverse.
    riga = next(line for line in breakdown.lines if line.kind == "discount")
    assert riga.amount == -20.0
    assert "dichiarato da te" in riga.label


def test_servono_tutte_le_offerte_che_compongono_il_totale() -> None:
    """Il prezzo basso nasce da due tratte scontate in due modi: averne diritto
    su una sola non lo ottiene."""
    treno = _treno_con_tariffe_ridotte(FrecciaYOUNG=61.0, SENIOR=61.0)
    solo_young = Discount(
        name="Promo Young", scope="provider:trenitalia", value=5, offers=["FrecciaYOUNG"]
    )
    breakdown = cost.compute([treno], _query(solo_young))

    riga = next(line for line in breakdown.lines if line.kind == "discount")
    assert riga.amount == -5.0
    assert "dichiarato da te" in riga.label


def test_una_tariffa_ridotta_piu_cara_del_prezzo_esposto_viene_ignorata() -> None:
    treno = _treno_con_tariffe_ridotte(FrecciaYOUNG=120.0)
    tessera = Discount(
        name="Promo Young", scope="provider:trenitalia", value=0, offers=["FrecciaYOUNG"]
    )
    breakdown = cost.compute([treno], _query(tessera))

    assert breakdown.total == 100.0
    assert not [line for line in breakdown.lines if line.kind == "discount"]
