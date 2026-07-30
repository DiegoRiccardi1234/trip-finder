"""Grimaldi Lines, dal motore di preventivo del suo sito di prenotazione.

E' il primo traghetto di linea vero del progetto: gli altri tre (Liberty Lines,
Blu Navy, Ichnusa) arrivano dalla piattaforma Albatross e coprono solo
collegamenti con le isole minori.

Un solo endpoint, senza autenticazione, che risponde a comandi diversi secondo
il campo `f`:

  POST booking.grimaldi-lines.com/ajax.php  f=s1tR   elenco delle rotte servite
  POST booking.grimaldi-lines.com/ajax.php  f=s1sL   preventivo per una rotta

Restituisce HTML, non JSON: un calendario di caselle, una per partenza. Non e'
bello, ma e' stabile e non richiede un browser, quindi resta tier 1.

Tre cose che non si intuiscono dalla risposta:

  - **il prezzo e' del gruppo, non della persona.** Con due adulti la stessa
    partenza costa il doppio (29,80 contro 59,60): va diviso, perche' tutto il
    resto del progetto ragiona per passeggero.
  - **la sistemazione va chiesta al plurale.** Il campo accetta `CABIN|PONT`, e
    il preventivo torna con la piu' economica delle due; chiedendo solo `PONT`
    o solo `CABIN` la risposta e' vuota.
  - **il certificato non valida** sulle CA di sistema (`curl(60)`), quindi la
    richiesta va fatta con la verifica disattivata. Non c'e' nulla di segreto in
    transito: e' un preventivo pubblico, gli stessi dati che il sito mostra a
    chiunque.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from app.models import Fare, Leg, Mode, Node, NodeKind, node_distance_km
from app.orchestrator import cache
from app.providers import coverage
from app.providers.base import NotServed, Provider, ProviderError, SearchContext, city_key
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

AJAX_URL = "https://booking.grimaldi-lines.com/ajax.php"
BOOKING_URL = "https://www.grimaldi-lines.com/"

HEADERS = {
    "Accept": "*/*",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://booking.grimaldi-lines.com",
    "Referer": "https://booking.grimaldi-lines.com/",
}

#: Emissioni per passeggero-km su un traghetto di linea con cabine. Sta fra il
#: pullman e l'aereo: la nave consuma molto ma trasporta molte persone.
CO2_KG_PER_KM = 0.115

#: Una casella del calendario: identificatore, prezzo e la nota con la nave e
#: gli orari. Sono tre informazioni separate nel markup, unite qui dal fatto
#: che stanno nello stesso blocco `cal_day`.
DAY_BLOCK_RE = re.compile(
    r'id="leg1-(?P<code>[A-Z0-9]+)".*?'
    r'day_number">\s*(?P<price>[\d.,]+)\s*&euro;.*?'
    r"moreInfo\('(?P<info>[^']*)'\)",
    re.S,
)
INFO_RE = re.compile(
    r"Vessel:\s*(?P<vessel>[^<]*)<br>\s*Dep\.:\s*(?P<dep>[^<]*)<br>\s*Arr\.:\s*(?P<arr>[^<]*)",
    re.I,
)
ROUTE_OPTION_RE = re.compile(
    r"<option\s+value='(?P<code>[A-Z]{5}-[A-Z]{5})'[^>]*>(?P<label>[^<]+)</option>"
)
NO_QUOTE = "no quotation found"

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return " ".join(cleaned.split())


@register
class GrimaldiLines(Provider):
    id = "grimaldi"
    name = "Grimaldi Lines"
    mode = Mode.FERRY
    tier = 1
    granularity = "city"
    website = BOOKING_URL
    sample_route = ("Napoli", "Palermo")

    def supports_node(self, node: Node) -> bool:
        return node.kind in {NodeKind.PORT, NodeKind.CITY}

    def can_serve(self, origin: Node, destination: Node) -> bool:
        if origin.id == destination.id or city_key(origin) == city_key(destination):
            return False
        return self.supports_node(origin) and self.supports_node(destination)

    # ------------------------------------------------------------- anagrafica

    async def _routes(self, ctx: SearchContext) -> dict[str, Any]:
        """Rotte servite, in cache per un mese.

        L'elenco porta sia i codici dei porti sia i loro nomi, quindi da una
        sola richiesta escono la traduzione nome-codice e la copertura."""
        cached = await cache.get("grimaldi:routes")
        if cached is not None:
            return cached

        try:
            response = await ctx.http.post(
                AJAX_URL,
                data={"f": "s1tR", "l": "en", "c": "GRI", "p1": "", "p2": "", "p3": ""},
                headers=HEADERS,
                # La catena di booking.grimaldi-lines.com non valida sulle CA di
                # sistema (`curl(60)`), vedi il docstring in cima: un host solo,
                # un preventivo pubblico, nulla di riservato in transito.
                verify=False,
            )
        except HttpError as exc:
            raise ProviderError(f"elenco rotte non disponibile: {exc}") from exc

        pairs: list[tuple[str, str]] = []
        by_name: dict[str, str] = {}
        for match in ROUTE_OPTION_RE.finditer(response.text):
            origin_code, dest_code = match.group("code").split("-")
            label = match.group("label")
            if " - " not in label:
                continue
            origin_name, dest_name = (part.strip() for part in label.split(" - ", 1))
            pairs.append((origin_code, dest_code))
            by_name.setdefault(_normalize(origin_name), origin_code)
            by_name.setdefault(_normalize(dest_name), dest_code)

        if not pairs:
            raise ProviderError("elenco rotte vuoto o in formato inatteso")

        served: dict[str, list[str]] = {}
        for origin_code, dest_code in pairs:
            served.setdefault(origin_code, []).append(dest_code)

        index = {"by_name": by_name, "served": served}
        await cache.set("grimaldi:routes", index, kind="resolve")

        for origin_code, destinations in served.items():
            await coverage.remember(self.id, origin_code, destinations)
        logger.debug("Grimaldi: %d porti, %d rotte", len(by_name), len(pairs))
        return index

    async def _code_for(self, node: Node, index: dict) -> str:
        by_name = index["by_name"]
        for key in _name_candidates(node):
            if key in by_name:
                return by_name[key]
        raise NotServed(f"Grimaldi non scala a {city_key(node)!r}")

    # ---------------------------------------------------------------- ricerca

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        index = await self._routes(ctx)
        origin_code = await self._code_for(origin, index)
        dest_code = await self._code_for(destination, index)

        served = index["served"].get(origin_code)
        if served is not None and dest_code not in served:
            raise NotServed(f"Grimaldi non collega {origin_code}-{dest_code}")

        pax = max(1, ctx.pax)
        leg = "|".join([
            origin_code,
            dest_code,
            ctx.date.strftime("%d/%m/%Y"),
            f"{origin_code} - {dest_code}",
        ])
        # sistemazioni ^ passeggeri (adulti, bambini, infanti, young, senior)
        p1 = f"{leg}^CABIN|PONT|{pax}|0|0|0|0|^^"
        try:
            response = await ctx.http.post(
                AJAX_URL,
                data={
                    "f": "s1sL", "l": "en", "c": "GRI", "p1": p1,
                    "p2": "", "p3": "", "p4": "|||", "p5": "||", "p6": "N",
                },
                headers=HEADERS,
                # Come sopra: il loro certificato non valida sulle CA di sistema.
                verify=False,
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc
        return response.text

    # ---------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, str):
            raise ProviderError("risposta inattesa: atteso il calendario in HTML")
        if NO_QUOTE in raw.lower():
            # Il motore lo dice a parole: nessun preventivo per quei criteri.
            # E' una risposta, non un guasto.
            return []

        pax = max(1, ctx.pax)
        legs: list[Leg] = []
        for match in DAY_BLOCK_RE.finditer(raw):
            leg = self._to_leg(match, origin, destination, ctx, pax)
            if leg is not None:
                legs.append(leg)
        if not legs and "cal_day" not in raw:
            raise ProviderError("il calendario delle partenze non c'e': markup cambiato")
        legs.sort(key=lambda item: item.depart)
        return legs

    def _to_leg(
        self, match: re.Match, origin: Node, destination: Node,
        ctx: SearchContext, pax: int,
    ) -> Leg | None:
        info = INFO_RE.search(match.group("info") or "")
        if info is None:
            return None

        depart = _stamp(info.group("dep"), ctx.date, origin, ctx)
        if depart is None or depart.date() != ctx.date:
            # La risposta contiene qualche giorno attorno a quello chiesto: le
            # altre partenze sono vere ma non sono quelle di questa ricerca, e
            # comporle in un itinerario datato altrove sarebbe un errore.
            return None
        arrive = _stamp(info.group("arr"), ctx.date, destination, ctx)
        if arrive is None:
            return None
        if arrive <= depart:
            arrive += timedelta(days=1)

        amount = _price(match.group("price"))
        fare = None
        notes = ["prezzo del posto piu' economico disponibile (poltrona o cabina)"]
        if amount is not None:
            fare = Fare(
                amount=round(amount / pax, 2),
                currency="EUR",
                fare_class="Poltrona/Cabina",
                refundable=False,
                included_cabin_bags=1,
                included_checked_bags=1,
            )
        else:
            notes.append("prezzo non esposto per questa partenza")

        distance_km = node_distance_km(origin, destination)
        return Leg(
            provider=self.id,
            mode=Mode.FERRY,
            origin=origin,
            destination=destination,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=(info.group("vessel") or "").strip().replace("_", " ") or None,
            fare=fare,
            booking_url=BOOKING_URL,
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            notes=notes,
        )


def _name_candidates(node: Node) -> list[str]:
    candidates: list[str] = []
    for raw in (node.city, city_key(node), node.name):
        key = _normalize(str(raw or ""))
        if not key or key in candidates:
            continue
        candidates.append(key)
        # "Napoli Porto" e "Olbia Isola Bianca" vanno ricondotti alla citta',
        # che e' il nome con cui la compagnia elenca i suoi scali.
        words = key.split()
        for size in (2, 1):
            if len(words) > size:
                shorter = " ".join(words[:size])
                if shorter not in candidates:
                    candidates.append(shorter)
    return candidates


def _price(text: str) -> float | None:
    try:
        return float(text.replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _stamp(text: str, reference: date, node: Node, ctx: SearchContext) -> datetime | None:
    """Converte "20 AUG 08:30" nel momento locale del porto.

    L'anno non c'e': si prende quello della ricerca, e se la data risultante
    cade molto prima si passa all'anno dopo. Serve a fine dicembre, quando una
    partenza del 30 e un arrivo del 1 gennaio appartengono ad anni diversi."""
    parts = (text or "").strip().split()
    if len(parts) < 3:
        return None
    try:
        day = int(parts[0])
        month = MONTHS[parts[1][:3].upper()]
        clock = datetime.strptime(parts[2], "%H:%M").time()
    except (ValueError, KeyError, IndexError):
        return None

    year = reference.year
    if month < reference.month - 6:
        year += 1
    try:
        return ctx.local_dt(node, date(year, month, day), clock)
    except ValueError:
        return None
