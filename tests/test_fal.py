"""Le regole di validita' di FAL, che decidono se un treno esiste quel giorno.

Sono la parte piu' delicata di un orario statico. Un adapter live che sbaglia
restituisce una lista vuota; uno statico che sbaglia propone con sicurezza un
treno che non partira' mai, e l'utente lo scopre in stazione.

Le condizioni vengono dal manifesto ufficiale, citate per esteso qui sotto.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import registry
from app.providers.rail import fal
from tests._datasets import needs_datasets


def test_niente_servizio_la_domenica() -> None:
    """Dal manifesto: "Il Servizio Ferroviario e' sospeso nei giorni di domenica
    e festivi"."""
    domenica = date(2026, 8, 16)
    assert domenica.weekday() == 6
    runs, reasons = fal.runs_on(domenica)
    assert runs == []
    assert any("domenica" in r for r in reasons)


def test_niente_servizio_a_ferragosto() -> None:
    runs, reasons = fal.runs_on(date(2026, 8, 15))
    assert runs == []
    assert any("festiv" in r for r in reasons)


def test_treni_estivi_soppressi_spariscono() -> None:
    """Dal manifesto: "(1) - Treni soppressi dal 27 luglio al 29 agosto 2026".

    Cade in pieno agosto, cioe' esattamente quando si scende al Sud: proporre
    un treno marcato (1) in quel periodo sarebbe l'errore piu' costoso che
    questo adapter possa fare."""
    agosto, settembre = date(2026, 8, 14), date(2026, 9, 15)

    estate, motivi = fal.runs_on(agosto)
    fuori, _ = fal.runs_on(settembre)

    assert estate, "in un venerdi' di agosto qualche corsa deve esserci"
    assert any(fal.SUPPRESSED_FLAG in (r.get("flag") or "") for r in fuori)
    assert not any(fal.SUPPRESSED_FLAG in (r.get("flag") or "") for r in estate)
    assert len(estate) < len(fuori)
    assert any("soppress" in m for m in motivi)


@pytest.mark.skipif(not fal.load_schedule().get("runs"), reason="orario FAL non generato")
@needs_datasets
def test_bari_matera_come_sul_manifesto() -> None:
    """Confronto diretto con il PDF: il bus 101 parte da Bari alle 4.14, arriva
    ad Altamura alle 5.36; il treno 1 riparte alle 5.37 e arriva a Matera
    Centrale alle 6.50. Il motore deve ricostruire quella coincidenza."""
    from app.geo.datasets import load_index
    from app.providers.base import SearchContext

    index = load_index()
    bari = index.by_id["ov:fal-bari-centrale"]
    matera = index.by_id["ov:fal-matera-centrale"]

    provider = registry.get("fal")
    ctx = SearchContext(date=date(2026, 9, 15))
    legs = provider.parse(fal.load_schedule(), bari, matera, ctx)

    orari = {(leg.depart.strftime("%H:%M"), leg.arrive.strftime("%H:%M")) for leg in legs}
    assert ("04:14", "06:50") in orari, f"orari ricostruiti: {sorted(orari)[:8]}"

    prima = next(l for l in legs if l.depart.strftime("%H:%M") == "04:14")
    # Un cambio ad Altamura, ma un solo biglietto: non e' un rischio di
    # coincidenza persa, ed e' giusto che non venga contato come tale.
    assert prima.internal_changes == 1
    assert prima.provider == "fal"
    # Il prezzo viene dal listino a fasce: Bari-Matera sono 70 km tassabili,
    # che nel modulo tariffe della Regione Puglia costano 6,20 euro. Non e'
    # una stima, ed e' giusto che non sia dichiarata tale.
    assert prima.fare is not None
    assert prima.fare.amount == 6.20


def test_il_prezzo_fal_esce_dal_listino_a_fasce() -> None:
    """La tariffa non dipende dalla tratta ma dalla fascia chilometrica.

    Bari-Altamura sono 45 km tassabili e la prima fascia che li copre e' quella
    dei 45: costa 4,00. Sopra l'ultima fascia pubblicata non si estrapola."""
    fares = fal.load_schedule().get("fares") or {}
    assert fares.get("bands"), "listino assente: rilancia scripts/build_fal_schedule.py"

    assert fal.price_for(fares, 45) == 4.00
    assert fal.price_for(fares, 41) == 4.00  # si paga la fascia superiore
    assert fal.price_for(fares, 10) == 1.30
    assert fal.price_for(fares, 5000) is None

    prezzo, km = fal.fare_between(
        fares, "ov:fal-bari-centrale", "ov:fal-altamura"
    )
    assert (prezzo, km) == (4.00, 45)
    # La tariffa non dipende dal verso di marcia.
    assert fal.fare_between(fares, "ov:fal-altamura", "ov:fal-bari-centrale") == (4.00, 45)


def test_le_stazioni_del_manifesto_diventano_nodi() -> None:
    assert fal.node_id_for("Bari C.le (Terminal BUS FS via Capruzzi)") == "ov:fal-bari-centrale"
    assert fal.node_id_for("Matera C.le") == "ov:fal-matera-centrale"
    assert fal.node_id_for("Altamura (partenza stazione FAL)") == "ov:fal-altamura"
    # Una fermata che non sappiamo mappare non deve inventarsi un nodo.
    assert fal.node_id_for("Pescariello") is None
