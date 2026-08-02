"""Cambi valuta, per confrontare prezzi che non sono nella stessa moneta.

Serve da quando la ricerca esce dall'Europa. Fino a ieri ogni tariffa era in
euro e il totale poteva dichiararlo per costruzione; adesso una gamba puo'
arrivare in sterline o in franchi, e sommarla com'e' produrrebbe un numero
sbagliato del quindici o venti per cento — presentato come certo, e per giunta
usato dalla classifica per decidere **quale** viaggio consigliare.

Tre scelte, tutte per la stessa ragione:

  - **La fonte e' la BCE**, il listino di riferimento giornaliero: pubblico,
    senza chiave, un chilobyte e mezzo, ventinove valute. Un tasso inventato o
    scritto a mano nel codice sarebbe esattamente il tipo di numero che questo
    progetto non si permette, e invecchierebbe in silenzio.
  - **I tassi stanno in memoria e la conversione e' sincrona.** `cost.compute`
    gira decine di migliaia di volte dentro una ricerca: non puo' aspettare la
    rete. Si scarica all'avvio e ogni dodici ore, come il catalogo tessere.
  - **Quello che non si sa convertire non si converte**, e chi lo riceve lo
    sa: `converti` dice sempre se il numero che restituisce e' stato cambiato
    davvero. Meglio una riga dichiarata incerta di un totale falso.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

#: Il listino di riferimento della Banca centrale europea, aggiornato ogni
#: giorno lavorativo verso le 16:00 CET.
SORGENTE = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

_NS = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}

#: I tassi cambiano una volta al giorno: riscaricarli piu' spesso non porta
#: niente, e dodici ore coprono anche chi tiene il programma aperto tutto il
#: giorno.
TTL = 12 * 3600

#: Quante unita' di valuta per un euro. Vuoto finche' non si scarica: con la
#: mappa vuota `converti` dice «non convertito» e nessuno somma niente di
#: sbagliato.
_TASSI: dict[str, float] = {}
_SCARICATO_IL: float = 0.0
_DATA_LISTINO: str = ""


def da_riscaricare() -> bool:
    return not _TASSI or (time.time() - _SCARICATO_IL) > TTL


def aggiornato_il() -> str:
    """La data del listino in uso, come la dichiara la BCE. Vuota se non c'e'."""
    return _DATA_LISTINO


def converti(amount: float, currency: str | None) -> tuple[float, bool]:
    """L'importo in euro, e se e' stato davvero convertito.

    Il secondo valore non e' un dettaglio: chi lo riceve lo usa per dichiarare
    la riga come stimata invece di far passare per certo un numero che non e'
    stato cambiato."""
    codice = (currency or "EUR").upper()
    if codice == "EUR":
        return amount, True
    tasso = _TASSI.get(codice)
    if not tasso:
        return amount, False
    return round(amount / tasso, 2), True


def note(currency: str | None) -> str | None:
    """Cosa dire di un prezzo che non era in euro. `None` se era gia' euro."""
    codice = (currency or "EUR").upper()
    if codice == "EUR":
        return None
    if codice in _TASSI:
        return f"prezzo dell'operatore in {codice}, convertito al cambio BCE del {_DATA_LISTINO}"
    return f"prezzo in {codice}: nessun cambio disponibile, la cifra non e' confrontabile"


def _leggi(xml: str) -> tuple[dict[str, float], str]:
    radice = ET.fromstring(xml)
    for cubo in radice.iter():
        if not cubo.tag.endswith("Cube") or not cubo.get("time"):
            continue
        tassi: dict[str, float] = {}
        for voce in cubo.findall("e:Cube", _NS):
            codice, tasso = voce.get("currency"), voce.get("rate")
            if not codice or not tasso:
                continue
            try:
                tassi[codice.upper()] = float(tasso)
            except ValueError:
                continue
        return tassi, cubo.get("time") or ""
    return {}, ""


async def aggiorna() -> bool:
    """Riscarica il listino. Vero se adesso c'e' qualcosa da usare.

    Silenzioso quando non riesce: senza rete restano i tassi di prima, e senza
    nemmeno quelli si continua a non convertire — che e' il comportamento
    giusto, non un guasto."""
    global _TASSI, _SCARICATO_IL, _DATA_LISTINO
    if not da_riscaricare():
        return bool(_TASSI)

    from app.providers.http_client import HttpError, get_http_client

    try:
        risposta = await get_http_client().get(SORGENTE, retries=1)
        testo = risposta.text
    except HttpError as exc:
        logger.info("cambi non scaricati (%s): tengo quelli che ho", exc)
        return bool(_TASSI)

    try:
        tassi, giorno = _leggi(testo)
    except ET.ParseError as exc:
        logger.warning("listino cambi illeggibile (%s): tengo quelli che ho", exc)
        return bool(_TASSI)

    if not tassi:
        logger.warning("listino cambi vuoto: tengo quelli che ho")
        return bool(_TASSI)

    _TASSI, _DATA_LISTINO, _SCARICATO_IL = tassi, giorno, time.time()
    logger.info("cambi aggiornati: %d valute, listino del %s", len(tassi), giorno)
    return True


def imposta_per_test(tassi: dict[str, float], giorno: str = "2026-01-01") -> None:
    """Usato dai test, che non devono toccare la rete."""
    global _TASSI, _DATA_LISTINO, _SCARICATO_IL
    _TASSI, _DATA_LISTINO, _SCARICATO_IL = dict(tassi), giorno, time.time()
