"""La copia che sostituisce l'installazione, e cosa deve non toccare.

Le regole qui sotto vengono da un aggiornamento fallito davvero, il 2026-08-01:
l'aggiornatore stava riscrivendo sé stesso mentre girava, e Windows glielo ha
impedito con `PermissionError [WinError 32]` lasciando l'installazione a metà.
Un aggiornamento che fallisce a metà è peggio di uno che non parte.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import update_sync


def _albero(radice: Path, file: dict[str, str]) -> None:
    for percorso, contenuto in file.items():
        destinazione = radice / percorso
        destinazione.parent.mkdir(parents=True, exist_ok=True)
        destinazione.write_text(contenuto, encoding="utf-8")


def test_i_dati_dell_utente_non_si_toccano(tmp_path) -> None:
    """Il database, le chiavi e i log sono suoi. Un aggiornamento che li
    sovrascrive gli fa perdere il profilo per aggiungere una funzione."""
    nuovo, installato = tmp_path / "nuovo", tmp_path / "installato"
    _albero(nuovo, {
        "TripFinder.exe": "versione nuova",
        "_internal/libreria.dll": "nuova",
        "data/trip_finder.db": "QUESTO NON DEVE ARRIVARE",
    })
    _albero(installato, {
        "TripFinder.exe": "versione vecchia",
        "data/trip_finder.db": "il database dell'utente",
        "data/local_secrets.json": "le sue chiavi",
        ".env": "la sua configurazione",
    })

    scritti = update_sync.sincronizza(origine=nuovo, destinazione=installato)

    assert scritti == 2  # l'eseguibile e la libreria, non il database
    assert (installato / "TripFinder.exe").read_text(encoding="utf-8") == "versione nuova"
    assert (installato / "data" / "trip_finder.db").read_text(encoding="utf-8") == "il database dell'utente"
    assert (installato / "data" / "local_secrets.json").exists()
    assert (installato / ".env").read_text(encoding="utf-8") == "la sua configurazione"


def test_un_dataset_nuovo_arriva_anche_a_chi_aggiorna(tmp_path) -> None:
    """Proteggere `data/` per intero proteggeva anche da noi stessi.

    La 0.5.1 metteva il gazetteer mondiale nello zip, ma sta in `data/` — e
    `data/` non si tocca. Verificato il 2026-08-17 su un'installazione vera
    appena aggiornata a 0.6.0: «Londra» rispondeva ancora **Ondara, in Spagna**,
    perche' il file non era mai arrivato. La correzione valeva solo per chi
    installava da zero, cioe' per nessuno di quelli che gia' usavano il
    programma."""
    nuovo, installato = tmp_path / "nuovo", tmp_path / "installato"
    _albero(nuovo, {
        "TripFinder.exe": "versione nuova",
        "data/cities15000.txt": "le citta' del mondo",
        "data/trip_finder.db": "QUESTO NON DEVE ARRIVARE",
    })
    _albero(installato, {
        "TripFinder.exe": "versione vecchia",
        "data/trip_finder.db": "il database dell'utente",
    })

    update_sync.sincronizza(origine=nuovo, destinazione=installato)

    assert (installato / "data" / "cities15000.txt").read_text(encoding="utf-8") == "le citta' del mondo"
    # E l'altra meta' della regola regge: quello che c'era resta suo.
    assert (installato / "data" / "trip_finder.db").read_text(encoding="utf-8") == "il database dell'utente"


def test_non_si_riscrive_addosso(tmp_path, monkeypatch) -> None:
    """È il difetto che ha fatto fallire l'aggiornamento del 2026-08-01: fra i
    file da sostituire c'è l'aggiornatore stesso, e su Windows un eseguibile in
    esecuzione è bloccato."""
    nuovo, installato = tmp_path / "nuovo", tmp_path / "installato"
    _albero(nuovo, {"Aggiorna.exe": "nuovo", "TripFinder.exe": "nuovo"})
    _albero(installato, {"Aggiorna.exe": "quello che sta girando"})
    monkeypatch.setattr(sys, "executable", str(installato / "Aggiorna.exe"))

    update_sync.sincronizza(origine=nuovo, destinazione=installato)

    assert (installato / "Aggiorna.exe").read_text(encoding="utf-8") == "quello che sta girando"
    assert (installato / "TripFinder.exe").read_text(encoding="utf-8") == "nuovo"


def test_un_file_bloccato_si_riprova_prima_di_arrendersi(tmp_path, monkeypatch) -> None:
    """Defender su un archivio appena scompattato può tenersi un file dieci o
    venti secondi. Arrendersi al primo tentativo vuol dire fallire per una
    scansione."""
    tentativi = {"n": 0}
    vero = update_sync.shutil.copy2

    def a_volte(origine, destinazione):
        tentativi["n"] += 1
        if tentativi["n"] < 3:
            raise PermissionError(13, "occupato")
        return vero(origine, destinazione)

    monkeypatch.setattr(update_sync.shutil, "copy2", a_volte)
    monkeypatch.setattr(update_sync.time, "sleep", lambda _s: None)

    nuovo, installato = tmp_path / "nuovo", tmp_path / "installato"
    _albero(nuovo, {"TripFinder.exe": "nuovo"})
    installato.mkdir()

    assert update_sync.sincronizza(origine=nuovo, destinazione=installato) == 1
    assert tentativi["n"] == 3


def test_se_resta_bloccato_lo_dice_col_nome_del_file(tmp_path, monkeypatch) -> None:
    """Il messaggio generico non serve a nessuno: sapere quale file è rimasto
    bloccato dice se è l'eseguibile, una libreria o l'antivirus."""
    def sempre_occupato(origine, destinazione):
        raise PermissionError(13, "occupato")

    monkeypatch.setattr(update_sync.shutil, "copy2", sempre_occupato)
    monkeypatch.setattr(update_sync.time, "sleep", lambda _s: None)

    nuovo, installato = tmp_path / "nuovo", tmp_path / "installato"
    _albero(nuovo, {"_internal/bloccata.dll": "x"})
    installato.mkdir()

    with pytest.raises(PermissionError, match="bloccata.dll"):
        update_sync.sincronizza(origine=nuovo, destinazione=installato)


def test_senza_la_cartella_di_partenza_non_si_finge_di_aggiornare(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        update_sync.sincronizza(origine=tmp_path / "manca", destinazione=tmp_path / "dove")
