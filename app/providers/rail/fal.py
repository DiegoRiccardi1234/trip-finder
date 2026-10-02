"""Ferrovie Appulo Lucane: l'unico servizio ferroviario che arriva a Matera.

FAL non ha nessuna API. Pubblica manifesti in PDF, e da quelli
`scripts/build_fal_schedule.py` ricava `data/fal_schedule.json`, che questo
adapter legge. Quindi niente rete, niente prezzi live: **orari statici**, ed e'
dichiarato come tale.

Le condizioni contano quanto gli orari, e sono scritte nel manifesto:

  - il servizio ferroviario e' sospeso la domenica e nei festivi;
  - i treni marcati `(1)` sono soppressi nella finestra estiva del manifesto;
  - alcuni collegamenti Gravina-Potenza sono coperti da autobus sostitutivi.

Proporre un treno che quel giorno non parte e' peggio che non proporlo, quindi
questi filtri si applicano prima di produrre qualunque gamba.

Un viaggio Bari-Matera spesso richiede un cambio ad Altamura, perche' la linea e'
spezzata li'. Resta **un biglietto solo**, quindi qui diventa una gamba unica con
`internal_changes = 1`: non ha il rischio di coincidenza persa dei biglietti
separati, ma il cambio c'e' e va detto.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import orjson

from app.models import Fare, Leg, Mode, Node, NodeKind
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.registry import register

logger = logging.getLogger(__name__)

SCHEDULE = Path(__file__).parent / "data" / "fal_schedule.json"

#: Fermate del manifesto tradotte nei nodi del progetto. La chiave e' cercata
#: come sottostringa nel nome che compare sul PDF.
STATION_KEYS: tuple[tuple[str, str], ...] = (
    ("bari c.le", "ov:fal-bari-centrale"),
    ("matera c.le", "ov:fal-matera-centrale"),
    ("villa longo", "ov:fal-matera-villa-longo"),
    ("altamura", "ov:fal-altamura"),
    ("gravina", "ov:fal-gravina"),
)

#: "(1) - Treni soppressi dal 27 luglio al 29 agosto 2026", dal manifesto.
SUPPRESSED_FLAG = "1"
SUPPRESSED_FROM = date(2026, 7, 27)
SUPPRESSED_TO = date(2026, 8, 29)

#: Festivi italiani a data fissa. Pasquetta si calcola separatamente.
FIXED_HOLIDAYS = {
    (1, 1), (1, 6), (4, 25), (5, 1), (6, 2),
    (8, 15), (11, 1), (12, 8), (12, 25), (12, 26),
}

#: Margine minimo per cambiare treno ad Altamura restando sullo stesso biglietto.
MIN_CHANGE_MINUTES = 1
MAX_CHANGE_MINUTES = 90


@lru_cache(maxsize=1)
def load_schedule() -> dict:
    if not SCHEDULE.exists():
        return {"runs": [], "notes": []}
    return orjson.loads(SCHEDULE.read_bytes())


def node_id_for(station_name: str) -> str | None:
    lowered = station_name.lower()
    for key, node_id in STATION_KEYS:
        if key in lowered:
            return node_id
    return None


def runs_on(day: date) -> tuple[list[dict], list[str]]:
    """Corse valide quel giorno, piu' i motivi per cui altre sono escluse."""
    schedule = load_schedule()
    reasons: list[str] = []

    if day.weekday() == 6:
        return [], ["il servizio ferroviario FAL non circola la domenica"]
    if (day.month, day.day) in FIXED_HOLIDAYS:
        return [], ["il servizio ferroviario FAL non circola nei giorni festivi"]
    if day == _easter_sunday(day.year) + timedelta(days=1):
        return [], ["il servizio ferroviario FAL non circola a Pasquetta"]

    valid_from = schedule.get("valid_from")
    if valid_from and day < date.fromisoformat(valid_from):
        return [], [f"il manifesto FAL e' valido dal {valid_from}: data precedente non verificata"]

    summer = _in_summer_window(day, schedule)
    if summer:
        reasons.append(
            "alcuni treni sono soppressi nella finestra estiva del manifesto e non compaiono"
        )

    valid = [
        run
        for run in schedule.get("runs") or []
        if not (summer and SUPPRESSED_FLAG in (run.get("flag") or ""))
    ]
    return valid, reasons


def _in_summer_window(day: date, schedule: dict | None = None) -> bool:
    window = (schedule or {}).get("suppressed_window")
    if window is not None:
        start = date.fromisoformat(window["from"])
        end = date.fromisoformat(window["to"])
    else:
        # Compatibilita' con il manifesto di giugno 2026: una soppressione
        # datata non deve ricomparire automaticamente ogni anno.
        start, end = SUPPRESSED_FROM, SUPPRESSED_TO
    return start <= day <= end


def _easter_sunday(year: int) -> date:
    """Computus gregoriano: Pasqua, da cui ricavare il lunedi' festivo."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


# -------------------------------------------------------------------- prezzi


def pair_key(first: str, second: str) -> str:
    """Chiave simmetrica: la tariffa non dipende dal verso di marcia."""
    return "|".join(sorted((first, second)))


def price_for(fares: dict, km: int) -> float | None:
    """Prezzo della corsa semplice per una distanza tassabile.

    FAL non ha una tariffa per tratta ma per **fascia**: si paga la prima
    fascia che copre i chilometri tassabili. Oltre l'ultima fascia pubblicata
    non si estrapola: si dichiara di non sapere, che e' vero."""
    bands = sorted((int(k), float(v)) for k, v in (fares.get("bands") or []))
    for limit, price in bands:
        if km <= limit:
            return price
    return None


def fare_between(fares: dict, origin_id: str, destination_id: str) -> tuple[float | None, int | None]:
    """Prezzo e distanza tassabile fra due stazioni, se il listino le conosce."""
    distances = fares.get("distances") or {}
    km = distances.get(pair_key(origin_id, destination_id))
    if km is None:
        return None, None
    return price_for(fares, int(km)), int(km)


@register
class FerrovieAppuloLucane(Provider):
    id = "fal"
    name = "Ferrovie Appulo Lucane"
    mode = Mode.RAIL
    tier = 1
    countries = frozenset({"IT"})
    website = "https://ferrovieappulolucane.it/tratta/orari/"
    sample_route = ("Bari", "Matera")
    #: Orari e tariffe da documento ufficiale, non da un motore di ricerca live.
    is_static = True

    @property
    def has_prices(self) -> bool:  # type: ignore[override]
        """Vero solo se il listino e' stato estratto davvero.

        Se il PDF delle tariffe cambia impaginazione lo script non lo scrive, e
        da quel momento le gambe FAL tornano senza prezzo: dirlo qui evita che
        il resto del motore le tratti come gratuite."""
        return bool((load_schedule().get("fares") or {}).get("bands"))

    def supports_node(self, node: Node) -> bool:
        return node.id in {node_id for _, node_id in STATION_KEYS}

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        """Nessuna rete: l'orario e' un file, gia' scaricato e verificato."""
        schedule = load_schedule()
        if not schedule.get("runs"):
            raise ProviderError(
                "orario FAL assente: esegui python scripts/build_fal_schedule.py"
            )
        return schedule

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        valid, reasons = runs_on(ctx.date)
        if not valid:
            # Nessuna corsa quel giorno e' una risposta, non un guasto: si dice
            # perche', invece di restituire una lista vuota muta.
            raise NotServed("; ".join(reasons) or "nessuna corsa quel giorno")

        journeys = self._direct(valid, origin, destination)
        journeys.extend(self._with_change(valid, origin, destination))

        legs: list[Leg] = []
        seen: set[tuple[int, int]] = set()
        for depart_min, arrive_min, label, changes in journeys:
            key = (depart_min, arrive_min)
            if key in seen:
                continue
            seen.add(key)
            legs.append(
                self._to_leg(origin, destination, ctx, depart_min, arrive_min, label, changes, reasons)
            )
        legs.sort(key=lambda leg: leg.depart)
        return legs

    # --------------------------------------------------------------- percorsi

    @staticmethod
    def _stop_times(run: dict, node_id: str) -> list[int]:
        return [
            stop["minutes"]
            for stop in run.get("stops") or []
            if node_id_for(stop["station"]) == node_id
        ]

    def _direct(
        self, runs: list[dict], origin: Node, destination: Node
    ) -> list[tuple[int, int, str, int]]:
        found: list[tuple[int, int, str, int]] = []
        for run in runs:
            departures = self._stop_times(run, origin.id)
            arrivals = self._stop_times(run, destination.id)
            if not departures or not arrivals:
                continue
            depart, arrive = min(departures), max(arrivals)
            if arrive > depart:
                found.append((depart, arrive, str(run.get("label") or "?"), 0))
        return found

    def _with_change(
        self, runs: list[dict], origin: Node, destination: Node
    ) -> list[tuple[int, int, str, int]]:
        """Viaggi con un cambio, tipicamente ad Altamura.

        La linea Bari-Matera e' spezzata: la prima parte e' servita da autobus
        sostitutivi, la seconda dai treni. Sono due corse diverse ma un solo
        biglietto, quindi il risultato e' una gamba sola con un cambio."""
        found: list[tuple[int, int, str, int]] = []
        interchange_ids = [node_id for _, node_id in STATION_KEYS]

        for first in runs:
            departures = self._stop_times(first, origin.id)
            if not departures:
                continue
            for hub_id in interchange_ids:
                if hub_id in (origin.id, destination.id):
                    continue
                hub_arrivals = self._stop_times(first, hub_id)
                if not hub_arrivals:
                    continue
                for second in runs:
                    if second is first:
                        continue
                    hub_departures = self._stop_times(second, hub_id)
                    arrivals = self._stop_times(second, destination.id)
                    if not hub_departures or not arrivals:
                        continue
                    depart, hub_in = min(departures), max(hub_arrivals)
                    hub_out, arrive = min(hub_departures), max(arrivals)
                    gap = hub_out - hub_in
                    if hub_in <= depart or arrive <= hub_out:
                        continue
                    if not (MIN_CHANGE_MINUTES <= gap <= MAX_CHANGE_MINUTES):
                        continue
                    label = f"{first.get('label')} + {second.get('label')}"
                    found.append((depart, arrive, label, 1))
        return found

    # ------------------------------------------------------------------ gamba

    def _to_leg(
        self,
        origin: Node,
        destination: Node,
        ctx: SearchContext,
        depart_min: int,
        arrive_min: int,
        label: str,
        changes: int,
        reasons: list[str],
    ) -> Leg:
        depart = ctx.local_dt(origin, ctx.date, time(depart_min // 60 % 24, depart_min % 60))
        if depart_min >= 24 * 60:
            depart += timedelta(days=1)
        arrive = ctx.local_dt(
            destination, ctx.date, time(arrive_min // 60 % 24, arrive_min % 60)
        )
        if arrive_min >= 24 * 60:
            arrive += timedelta(days=1)
        if arrive <= depart:
            arrive += timedelta(days=1)

        notes = [
            "orario da manifesto ufficiale FAL: verifica prima di partire",
            *reasons,
        ]
        if changes:
            notes.append("biglietto unico, ma con un cambio lungo il percorso")

        fares = load_schedule().get("fares") or {}
        amount, km = fare_between(fares, origin.id, destination.id)
        fare = None
        if amount is not None:
            fare = Fare(
                amount=round(amount, 2),
                currency="EUR",
                fare_class="Corsa semplice",
                refundable=False,
                included_cabin_bags=1,
                included_checked_bags=1,
            )
            notes.append(
                f"tariffa regionale a fasce: {km} km tassabili, listino ufficiale"
            )
        else:
            notes.append("prezzo non nel listino: va verificato alla biglietteria")

        return Leg(
            provider=self.id,
            mode=Mode.RAIL,
            origin=origin,
            destination=destination,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=label,
            fare=fare,
            booking_url=self.website,
            internal_changes=changes,
            notes=notes,
        )

    async def sample_nodes(self, ctx: SearchContext) -> tuple[Node, Node] | None:
        """Nodi della tratta di prova presi dal nostro indice geografico."""
        from app.geo.datasets import load_index

        index = load_index()
        origin = index.by_id.get("ov:fal-bari-centrale")
        destination = index.by_id.get("ov:fal-matera-centrale")
        if origin is None or destination is None:
            return None
        return origin, destination
