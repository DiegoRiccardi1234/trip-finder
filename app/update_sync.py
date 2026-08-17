"""Copia una cartella sopra l'altra, senza toccare quello che e' dell'utente.

Sta in un modulo suo, senza effetti all'import, per due motivi: lo importa
`scripts/updater.py`, che gira come programma a parte, e cosi' si puo' provare
senza costruire un eseguibile.

Le regole scritte qui vengono da un aggiornamento fallito davvero, il
2026-08-01: `PermissionError [WinError 32] Il file e' utilizzato da un altro
processo`. Su Windows i file restano bloccati per qualche secondo dopo che il
processo che li teneva e' uscito, e l'antivirus ne blocca altri mentre scansiona
un archivio appena scompattato. Un aggiornatore che si arrende al primo file
lascia l'installazione a meta', che e' peggio di non aver aggiornato.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

#: Roba dell'utente, mai sovrascritta: il database, il profilo, le chiavi, i
#: log, i profili del browser. Il confronto e' sulla prima parte del percorso,
#: quindi `data/qualunque/cosa` e' protetto perche' lo e' `data`.
DA_NON_TOCCARE = ("data", ".env", ".env.local", ".browser-profiles")

#: Quanto riprovare prima di arrendersi su un file bloccato. In tutto una
#: trentina di secondi: Defender su un archivio appena scompattato puo'
#: tenerselo dieci o venti secondi, e arrendersi prima vuol dire fallire per
#: una scansione.
ATTESE = (1.0, 2.0, 4.0, 8.0, 16.0)


def _protetto(relativo: Path, arrivo: Path) -> bool:
    """Vero se questo file e' dell'utente e non va scritto.

    Dentro `data/` c'e' roba di due proprietari diversi: il database, le chiavi,
    i cookie e i log sono suoi; i dataset geografici sono nostri e viaggiano
    nello zip. Proteggere la cartella per intero proteggeva anche **da noi**: la
    0.5.1 metteva il gazetteer mondiale nel pacchetto, ma chi aggiornava non lo
    riceveva mai, e su un'installazione aggiornata alla 0.6.0 «Londra»
    rispondeva ancora Ondara, in Spagna. La correzione valeva solo per chi
    installava da zero.

    La regola: dentro `data/` si scrive **solo quello che ancora non c'e'**. Un
    file nuovo arriva, uno esistente non viene toccato — e siccome il pacchetto
    non contiene nessun file dell'utente, non c'e' modo che questo ne
    sovrascriva uno. Per rinfrescare un dataset gia' presente c'e'
    `scripts/fetch_datasets.py`, che e' un gesto voluto."""
    parti = relativo.parts
    if not parti or parti[0] not in DA_NON_TOCCARE:
        return False
    if parti[0] == "data" and not arrivo.exists():
        return False
    return True


def _e_questo_programma(destinazione: Path) -> bool:
    """Vero se il file di arrivo e' l'eseguibile che sta girando adesso.

    Cintura e bretelle: chi lancia l'aggiornatore ne mette una copia nel
    temporaneo proprio perche' quello installato resti libero, ma se quella
    copia non riesce non si deve comunque provare a riscrivere sé stessi
    mentre si e' in esecuzione — che e' il primo modo in cui questo
    aggiornamento e' fallito."""
    try:
        return destinazione.resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _copia_insistendo(origine: Path, destinazione: Path) -> None:
    for attesa in ATTESE:
        try:
            shutil.copy2(origine, destinazione)
            return
        except PermissionError:
            time.sleep(attesa)
    try:
        shutil.copy2(origine, destinazione)
    except PermissionError as exc:
        # Il nome del file che e' rimasto bloccato vale piu' del messaggio
        # generico: dice se e' l'eseguibile, una libreria o roba dell'antivirus.
        raise PermissionError(
            exc.errno,
            f"{exc.strerror} (ancora bloccato dopo {len(ATTESE)} tentativi): {destinazione}",
        ) from exc


def sincronizza(*, origine: Path, destinazione: Path) -> int:
    """Copia tutto quello che sta in `origine` dentro `destinazione`.

    Restituisce quanti file ha scritto. Non cancella niente: quello che c'era
    e non c'e' piu' resta, ed e' la scelta prudente — un file di troppo non ha
    mai rotto niente, uno mancante si'."""
    if not origine.exists():
        raise FileNotFoundError(f"non trovo la cartella da installare: {origine}")
    destinazione.mkdir(parents=True, exist_ok=True)

    scritti = 0
    for file in origine.rglob("*"):
        if not file.is_file():
            continue
        relativo = file.relative_to(origine)
        arrivo = destinazione / relativo
        if _protetto(relativo, arrivo):
            continue
        if _e_questo_programma(arrivo):
            continue
        arrivo.parent.mkdir(parents=True, exist_ok=True)
        _copia_insistendo(file, arrivo)
        scritti += 1
    return scritti
