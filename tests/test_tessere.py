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


# --- lo scaricamento, che tiene fresco chi non aggiorna il programma ---------


def _scrivi(percorso, voci, generato="2026-12-01"):
    import json

    percorso.parent.mkdir(parents=True, exist_ok=True)
    percorso.write_text(
        json.dumps({"generato_il": generato, "tessere": voci}, ensure_ascii=False),
        encoding="utf-8",
    )


def _voci(quante=6):
    return [
        {"id": f"t{i}", "nome": f"Tessera {i}", "fonte": "https://esempio", "valore": 10}
        for i in range(quante)
    ]


def test_lo_scaricato_vince_su_quello_del_programma(tmp_path, monkeypatch) -> None:
    """Uno zip di agosto porterebbe il catalogo di agosto per sempre, e il 30
    novembre scadono due promo."""
    dentro, fuori = tmp_path / "dentro.json", tmp_path / "fuori.json"
    _scrivi(dentro, _voci(), generato="2026-08-01")
    _scrivi(fuori, _voci(7), generato="2026-12-01")
    monkeypatch.setattr(tessere, "CATALOGO", dentro)
    monkeypatch.setattr(tessere, "SCARICATO", fuori)
    tessere.tutte.cache_clear()

    assert len(tessere.tutte()) == 7
    assert tessere.aggiornato_il() == "2026-12-01"
    tessere.tutte.cache_clear()


def test_uno_scaricato_rotto_non_manda_giu_il_catalogo(tmp_path, monkeypatch) -> None:
    dentro, fuori = tmp_path / "dentro.json", tmp_path / "fuori.json"
    _scrivi(dentro, _voci())
    fuori.write_text("{non json", encoding="utf-8")
    monkeypatch.setattr(tessere, "CATALOGO", dentro)
    monkeypatch.setattr(tessere, "SCARICATO", fuori)
    tessere.tutte.cache_clear()

    assert len(tessere.tutte()) == 6  # torna quello che viaggia col programma
    tessere.tutte.cache_clear()


def test_si_controlla_prima_di_sostituire() -> None:
    """Un catalogo mezzo scaricato che prende il posto di uno buono e' peggio di
    uno vecchio, perche' quello vecchio almeno lo sai."""
    assert tessere._valido({"tessere": _voci()})
    assert not tessere._valido({"tessere": _voci(2)})            # troppo poche
    assert not tessere._valido({"tessere": [{"id": "x"}] * 6})   # senza nome ne' fonte
    assert not tessere._valido({"tessere": "non una lista"})
    assert not tessere._valido("nemmeno un oggetto")


async def test_una_fonte_muta_lascia_tutto_com_era(tmp_path, monkeypatch) -> None:
    from app.providers import http_client

    dentro, fuori = tmp_path / "dentro.json", tmp_path / "fuori.json"
    _scrivi(dentro, _voci(), generato="2026-08-01")
    monkeypatch.setattr(tessere, "CATALOGO", dentro)
    monkeypatch.setattr(tessere, "SCARICATO", fuori)
    tessere.tutte.cache_clear()

    async def morta(*args, **kwargs):
        raise http_client.HttpError("niente rete")

    monkeypatch.setattr(
        http_client, "get_http_client", lambda: type("F", (), {"get_json": morta})()
    )

    assert await tessere.aggiorna() is False
    assert not fuori.exists()
    assert len(tessere.tutte()) == 6
    tessere.tutte.cache_clear()


async def test_una_risposta_incompleta_non_sostituisce_niente(tmp_path, monkeypatch) -> None:
    from app.providers import http_client

    dentro, fuori = tmp_path / "dentro.json", tmp_path / "fuori.json"
    _scrivi(dentro, _voci(), generato="2026-08-01")
    monkeypatch.setattr(tessere, "CATALOGO", dentro)
    monkeypatch.setattr(tessere, "SCARICATO", fuori)
    tessere.tutte.cache_clear()

    async def monca(*args, **kwargs):
        return {"tessere": [{"id": "solo-uno"}]}

    monkeypatch.setattr(
        http_client, "get_http_client", lambda: type("F", (), {"get_json": monca})()
    )

    assert await tessere.aggiorna() is False
    assert not fuori.exists()
    tessere.tutte.cache_clear()


def test_non_si_riscarica_a_ogni_avvio(tmp_path, monkeypatch) -> None:
    """Le condizioni di trasporto cambiano qualche volta l'anno: una richiesta
    al giorno e' gia' molto piu' spesso del necessario."""
    fuori = tmp_path / "fuori.json"
    _scrivi(fuori, _voci())
    monkeypatch.setattr(tessere, "SCARICATO", fuori)

    import time as _time

    assert not tessere.da_riscaricare(_time.time())
    assert tessere.da_riscaricare(_time.time() + tessere.OGNI + 60)
