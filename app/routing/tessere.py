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
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOGO = Path(__file__).with_name("data") / "tessere.json"

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


@lru_cache
def tutte() -> tuple[Tessera, ...]:
    """Il catalogo, letto una volta sola.

    Un catalogo illeggibile non deve impedire l'avvio: le tessere restano una
    comodita', e chi le scrive a mano nel profilo puo' farlo comunque."""
    if not CATALOGO.exists():
        logger.warning("catalogo tessere assente: %s", CATALOGO)
        return ()
    try:
        contenuto = json.loads(CATALOGO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("catalogo tessere illeggibile (%s)", exc)
        return ()
    voci = contenuto.get("tessere") if isinstance(contenuto, dict) else None
    return tuple(Tessera(v) for v in voci or [] if isinstance(v, dict))


def aggiornato_il() -> str:
    if not CATALOGO.exists():
        return ""
    try:
        contenuto = json.loads(CATALOGO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(contenuto.get("generato_il", "")) if isinstance(contenuto, dict) else ""


def utilizzabili(oggi: date | None = None) -> list[Tessera]:
    """Quelle che oggi hanno senso proporre: non scadute e gia' partite."""
    return [t for t in tutte() if not t.scaduta(oggi) and not t.non_ancora_valida(oggi)]
