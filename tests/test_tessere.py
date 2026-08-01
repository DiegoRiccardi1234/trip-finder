"""Il catalogo delle tessere: leggibile, coerente, e onesto sulle scadenze.

Il file lo genera `scripts/build_tessere.py` dalle fonti degli operatori, ma
resta versionato: se qualcuno lo modifica a mano e sbaglia una percentuale, il
danno non e' un errore, e' un prezzo piu' basso di quello vero in cima alla
classifica. Qui si controlla che il file regga prima di arrivarci.
"""

from __future__ import annotations

from datetime import date

from app.routing import tessere


def test_il_catalogo_si_legge_e_ha_dentro_quello_che_serve() -> None:
    catalogo = tessere.tutte()
    assert len(catalogo) >= 10

    for voce in catalogo:
        assert voce.id and voce.nome, f"voce senza nome: {voce.as_dict()}"
        # Senza fonte non si puo' riverificare, e una tessera che non si puo'
        # riverificare e' una tessera che invecchiera' in silenzio.
        assert voce.fonte, f"{voce.id}: senza fonte"
        assert voce.verificato_il, f"{voce.id}: senza data di verifica"
        if voce.tipo == "percent":
            assert 0 <= float(voce.valore) <= 100, f"{voce.id}: percentuale fuori scala"


def test_le_voci_estratte_portano_le_date_del_documento() -> None:
    """Le date non sono digitate: vengono dal PDF che vincola l'operatore.

    Quello della promo Young dice «valida per viaggi dal 5 aprile al 30 novembre
    2026», ed e' quello che deve esserci qui."""
    young = next(v for v in tessere.tutte() if v.id == "trenitalia-promo-young")

    assert young.estratto is True
    assert young.valido_da == "2026-04-05"
    assert young.valido_a == "2026-11-30"
    assert young.valore == 20.0


def test_una_tessera_ritirata_resta_in_catalogo() -> None:
    """La Carta Verde non si compra piu' dal 4 aprile 2026, ma chi ce l'ha la usa
    fino alla sua scadenza: toglierla dall'elenco vorrebbe dire non farla
    dichiarare a chi la sta ancora usando."""
    verde = next(v for v in tessere.tutte() if v.id == "trenitalia-carta-verde")

    assert verde.acquistabile is False
    assert verde.ritirata_dal == "2026-04-04"
    assert verde.valore == 10.0


def test_scaduta_e_stantia_sono_due_cose_diverse() -> None:
    """Scaduta vuol dire che sappiamo che non vale piu'. Stantia vuol dire che
    non sappiamo, e sono due frasi diverse da scrivere a schermo."""
    scaduta = tessere.Tessera({"id": "x", "valido_a": "2020-01-01", "estratto": True})
    assert scaduta.scaduta(date(2026, 8, 1))
    assert not scaduta.stantia(date(2026, 8, 1))

    vecchia = tessere.Tessera({"id": "y", "verificato_il": "2020-01-01", "estratto": False})
    assert not vecchia.scaduta(date(2026, 8, 1))
    assert vecchia.stantia(date(2026, 8, 1))

    # Le voci estratte non invecchiano a mano: le ricontrolla il lavoro
    # settimanale, e marcarle stantie sarebbe rumore.
    letta = tessere.Tessera({"id": "z", "verificato_il": "2020-01-01", "estratto": True})
    assert not letta.stantia(date(2026, 8, 1))


def test_utilizzabili_scarta_le_scadute_e_le_non_ancora_partite() -> None:
    oggi = date(2026, 8, 1)
    usabili = {t.id for t in tessere.utilizzabili(oggi)}

    assert "trenitalia-promo-young" in usabili  # valida fino al 30 novembre
    for voce in tessere.tutte():
        if voce.id in usabili:
            assert not voce.scaduta(oggi)
