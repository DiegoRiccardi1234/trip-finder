"""Il catalogo delle tessere e delle riduzioni, e come si legge.

Una tessera e' un dato che scade. Delle nove voci raccolte ad agosto 2026,
cinque portano una data dentro: la Carta Verde e la Carta Argento non si
comprano piu' dal 4 aprile, le promo Young e Senior valgono fino al 30 novembre,
lo sconto ISIC su FlixBus fino al 15 dicembre. Un elenco scritto a mano oggi
sbaglia in primavera, e sbaglia in silenzio — che e' il difetto peggiore, perche'
uno sconto inventato fa perdere il viaggio.

Per questo il catalogo non si digita: si estrae. Le Condizioni Generali di
Trasporto sono PDF pubblici, strutturati in paragrafi, e portano scritte dentro
le date di validita' e le percentuali. Sono anche il documento che vincola
l'operatore, quindi valgono piu' di qualunque pagina riassuntiva: le ricerche
dicevano che la Carta Verde e' sparita il 1 aprile, il PDF dice il 4.

Le voci che nessuno pubblica in forma leggibile restano scritte a mano, ma con
la fonte e la data in cui qualcuno l'ha guardata: cosi' invecchiano ad alta voce
invece che di nascosto.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

#: Quello che viaggia dentro il programma. Nel bundle e' la copia congelata al
#: momento della build, e senza di essa non ci sarebbe niente al primo avvio.
CATALOGO = Path(__file__).with_name("data") / "tessere.json"

#: Quello scaricato, usato se e' valido e piu' recente. Sta accanto al database perche' e'
#: un dato che cambia, non un pezzo del programma.
SCARICATO = DATA_DIR / "tessere.json"

#: Il file nel repository. Uno zip di agosto porterebbe per sempre il catalogo
#: di agosto, e il 30 novembre scadono due promo: senza questo, chi non
#: aggiorna il programma vedrebbe una scadenza passata come se fosse futura.
SORGENTE = (
    "https://raw.githubusercontent.com/DiegoRiccardi1234/trip-finder"
    "/main/app/routing/data/tessere.json"
)

#: Ogni quanto riprovare. Le condizioni di trasporto cambiano qualche volta
#: l'anno: una volta al giorno e' gia' molto piu' spesso del necessario.
OGNI = 24 * 3600

#: Oltre questo, una voce curata a mano va riguardata: non e' scaduta, e'
#: semplicemente vecchia, e la differenza va detta con parole diverse.
GIORNI_PRIMA_DI_RIGUARDARLA = 180


class Tessera:
    """Una voce del catalogo, con quello che serve a decidere se proporla."""

    __slots__ = ("dati",)

    def __init__(self, dati: dict[str, Any]) -> None:
        self.dati = dati

    def __getattr__(self, nome: str) -> Any:
        return self.dati.get(nome)

    def scaduta(self, oggi: date | None = None) -> bool:
        fine = _data(self.dati.get("valido_a"))
        return bool(fine and fine < (oggi or date.today()))

    def non_ancora_valida(self, oggi: date | None = None) -> bool:
        inizio = _data(self.dati.get("valido_da"))
        return bool(inizio and inizio > (oggi or date.today()))

    def stantia(self, oggi: date | None = None) -> bool:
        """Vero se nessuno la guarda da troppo tempo.

        Diverso da scaduta: non sappiamo che sia sbagliata, sappiamo che non
        sappiamo. Vale solo per le voci curate a mano — quelle estratte le
        ricontrolla il controllo settimanale."""
        if self.dati.get("estratto"):
            return False
        visto = _data(self.dati.get("verificato_il"))
        if visto is None:
            return True
        return ((oggi or date.today()) - visto).days > GIORNI_PRIMA_DI_RIGUARDARLA

    def as_dict(self) -> dict[str, Any]:
        return dict(self.dati)


def _data(valore: Any) -> date | None:
    if isinstance(valore, date):
        return valore
    if isinstance(valore, str) and valore.strip():
        try:
            return date.fromisoformat(valore.strip()[:10])
        except ValueError:
            return None
    return None


def _leggi(percorso: Path) -> dict[str, Any] | None:
    if not percorso.exists():
        return None
    try:
        contenuto = json.loads(percorso.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("catalogo tessere illeggibile in %s (%s)", percorso.name, exc)
        return None
    return contenuto if isinstance(contenuto, dict) else None


def _valido(contenuto: object) -> bool:
    """Abbastanza sano da sostituire quello che gia' abbiamo.

    Si controlla prima di scrivere, non dopo: un catalogo mezzo scaricato che
    prende il posto di uno buono e' peggio di uno vecchio, perche' quello
    vecchio almeno lo sai."""
    if not isinstance(contenuto, dict):
        return False
    voci = contenuto.get("tessere")
    if not isinstance(voci, list) or len(voci) < 5:
        return False
    return all(
        isinstance(v, dict) and v.get("id") and v.get("nome") and v.get("fonte")
        for v in voci
    )


def _generato_il(contenuto: dict[str, Any] | None) -> datetime | None:
    """Timestamp ISO completo, senza accettare il prefisso di metadati rotti.

    Il builder scrive una data: si considera mezzanotte UTC. Un catalogo che
    include l'ora si confronta con il suo fuso; un'ora senza fuso usa UTC.
    """
    valore = (contenuto or {}).get("generato_il")
    if not isinstance(valore, str):
        return None
    try:
        generato = datetime.fromisoformat(valore.strip())
    except ValueError:
        return None
    if generato.tzinfo is None:
        generato = generato.replace(tzinfo=timezone.utc)
    try:
        return generato.astimezone(timezone.utc)
    except OverflowError:
        return None


def _sorgente() -> Path:
    """Il catalogo valido piu' recente; nel dubbio resta quello del programma."""
    incluso, scaricato = _leggi(CATALOGO), _leggi(SCARICATO)
    if not _valido(scaricato):
        return CATALOGO
    if not _valido(incluso):
        return SCARICATO
    data_incluso, data_scaricato = _generato_il(incluso), _generato_il(scaricato)
    if data_scaricato is not None and (data_incluso is None or data_scaricato > data_incluso):
        return SCARICATO
    return CATALOGO


@lru_cache
def tutte() -> tuple[Tessera, ...]:
    """Il catalogo, letto una volta sola.

    Un catalogo illeggibile non deve impedire l'avvio: le tessere restano una
    comodita', e chi le scrive a mano nel profilo puo' farlo comunque."""
    contenuto = _leggi(_sorgente())
    if not _valido(contenuto):
        logger.warning("nessun catalogo tessere valido")
        return ()
    voci = contenuto.get("tessere")
    return tuple(Tessera(v) for v in voci or [] if isinstance(v, dict))


def aggiornato_il() -> str:
    contenuto = _leggi(_sorgente()) or {}
    return str(contenuto.get("generato_il", ""))


def da_riscaricare(adesso: float | None = None) -> bool:
    try:
        eta = (adesso or time.time()) - SCARICATO.stat().st_mtime
    except OSError:
        return True
    return eta > OGNI


async def aggiorna() -> bool:
    """Riscarica il catalogo dal repository. Vero se ne e' arrivato uno nuovo.

    Silenzioso quando non riesce: senza rete, dietro un proxy o con GitHub giu'
    resta quello di prima, che e' esattamente il comportamento giusto per un
    dato di comodo. Non e' silenzioso nel log, pero'."""
    if not da_riscaricare():
        return False

    from app.providers.http_client import HttpError, get_http_client

    try:
        contenuto = await get_http_client().get_json(
            SORGENTE, headers={"Accept": "application/json"}, retries=1
        )
    except HttpError as exc:
        logger.info("catalogo tessere non scaricato (%s): tengo quello che ho", exc)
        return False

    if not _valido(contenuto):
        logger.warning("catalogo tessere scaricato ma non utilizzabile: tengo quello che ho")
        return False

    attuale = _leggi(_sorgente())
    data_attuale = _generato_il(attuale) if _valido(attuale) else None
    data_nuova = _generato_il(contenuto)
    if data_nuova is None or (data_attuale is not None and data_nuova <= data_attuale):
        logger.info("catalogo tessere senza una data piu' recente: tengo quello che ho")
        return False

    vecchio = aggiornato_il()
    try:
        SCARICATO.parent.mkdir(parents=True, exist_ok=True)
        SCARICATO.write_text(
            json.dumps(contenuto, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("catalogo tessere non scrivibile (%s)", exc)
        return False

    tutte.cache_clear()
    nuovo = aggiornato_il()
    if nuovo != vecchio:
        logger.info("catalogo tessere aggiornato: %s -> %s", vecchio or "?", nuovo)
    return nuovo != vecchio


def utilizzabili(oggi: date | None = None) -> list[Tessera]:
    """Quelle che oggi hanno senso proporre: non scadute e gia' partite."""
    return [t for t in tutte() if not t.scaduta(oggi) and not t.non_ancora_valida(oggi)]
