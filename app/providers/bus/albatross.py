"""Albatross: la piattaforma di prenotazione condivisa da molti bus italiani.

Marino, Marozzi, Autolinee Federico, Liscio, Prontobus e altri non hanno un
sito ciascuno: hanno lo stesso motore, distribuito su un host per operatore
(`api.marinobus.it`, `albatrossapi.marozzivt.it`, ...). Stessa API, stessi
percorsi, stesso formato. Quindi qui c'e' **una** classe base e una riga per
operatore: aggiungerne uno nuovo costa tre righe, non un adapter.

Due endpoint, aperti e senza autenticazione:

  - `GET /Stops` -> l'intero catalogo fermate con coordinate, fuso e localita'
    di appartenenza. Da qui esce sia la traduzione nome-localita' sia la
    posizione reale dei capolinea, che serve a calcolare l'ultimo miglio.
  - `POST /search/s/{partenza}/{arrivo}/{dal}/{al}` -> le soluzioni. Prende i
    **nomi** delle localita', non gli identificatori, e un intervallo di date.

Un avvertimento sugli orari, spiegato sotto in `_local`: le date arrivano con
offset `+00:00` ma sono ore locali italiane.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.models import Fare, Leg, Mode, Node, NodeKind
from app.orchestrator import cache
from app.providers.base import NotServed, Provider, ProviderError, SearchContext, city_key
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

#: Parametri che il sito passa sempre. Vanno mandati tutti: senza, il motore
#: risponde 200 con zero soluzioni invece di segnalare l'errore.
SEARCH_QUERY = {
    "channel": "1",
    "reservationId": "",
    "inStaging": "false",
    "isReturn": "false",
    "excludeExternals": "false",
    "partial": "false",
    "caller": "",
    "changeDate": "false",
    "locale": "it",
    "coupon": "",
}
SEARCH_BODY = [{"fId": None, "extra": {}, "subGroupId": 0}]

CO2_KG_PER_KM = 0.029
DEFAULT_TZ = "Europe/Rome"


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return " ".join(cleaned.split())


class AlbatrossProvider(Provider):
    """Base comune. Le sottoclassi cambiano solo identita' e host."""

    mode = Mode.BUS
    tier = 1
    granularity = "city"
    countries = frozenset({"IT"})

    #: Host dell'API dell'operatore, con la barra finale.
    api_base: str = ""
    #: Sito di prenotazione, per costruire il collegamento all'acquisto.
    booking_base: str = ""

    def supports_node(self, node: Node) -> bool:
        if self.mode is Mode.FERRY:
            return node.kind in {NodeKind.PORT, NodeKind.CITY}
        return node.kind in {NodeKind.BUS_STOP, NodeKind.STATION, NodeKind.CITY}

    def can_serve(self, origin: Node, destination: Node) -> bool:
        if origin.id == destination.id or city_key(origin) == city_key(destination):
            return False
        return self.supports_node(origin) and self.supports_node(destination)

    # ------------------------------------------------------------- anagrafica

    async def _catalog(self, ctx: SearchContext) -> dict[str, Any]:
        """Fermate e localita' dell'operatore, in cache per un mese."""
        key = f"albatross:{self.id}:stops"
        cached = await cache.get(key)
        if cached is not None:
            return cached

        try:
            entries = await ctx.http.get_json(
                f"{self.api_base}Stops", headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(f"catalogo fermate non disponibile: {exc}") from exc
        if not isinstance(entries, list) or not entries:
            raise ProviderError("catalogo fermate vuoto o in formato inatteso")

        stops: dict[str, dict] = {}
        localities: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            stops[str(entry["id"])] = {
                "name": entry.get("shortDescription") or entry.get("description") or "",
                "lat": _as_float(entry.get("latitude")),
                "lon": _as_float(entry.get("longitude")),
                "tz": entry.get("timezone") or DEFAULT_TZ,
                "locality": entry.get("localityShortDescription") or "",
            }
            locality = entry.get("localityShortDescription")
            if locality:
                localities.setdefault(_normalize(str(locality)), str(locality))

        catalog = {"stops": stops, "localities": localities}
        await cache.set(key, catalog, kind="resolve")
        logger.debug("%s: %d fermate, %d localita'", self.id, len(stops), len(localities))
        return catalog

    async def sample_nodes(self, ctx: SearchContext) -> tuple[Node, Node] | None:
        """Fermate della `sample_route` prese dal catalogo dell'operatore.

        Molte di queste localita' (Costadedoi, Cianciana, Bisaccia Nuova) non
        sono nel nostro indice geografico, e farle passare dal resolver darebbe
        risultati assurdi. Qui si costruiscono direttamente dal catalogo, cosi'
        il controllo di salute misura l'adapter e nient'altro."""
        if not self.sample_route:
            return None
        catalog = await self._catalog(ctx)
        made = [
            _node_for_locality(catalog, label, self.mode) for label in self.sample_route
        ]
        if any(node is None for node in made):
            return None
        return made[0], made[1]  # type: ignore[return-value]

    async def _locality_for(self, node: Node, ctx: SearchContext) -> str:
        catalog = await self._catalog(ctx)
        localities = catalog["localities"]
        for key in _name_candidates(node):
            if key in localities:
                return localities[key]
        raise NotServed(f"{self.name} non serve {city_key(node)!r}")

    # ---------------------------------------------------------------- ricerca

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        from_name = await self._locality_for(origin, ctx)
        to_name = await self._locality_for(destination, ctx)
        if from_name == to_name:
            raise NotServed("origine e destinazione coincidono")

        day = ctx.date.isoformat()
        url = f"{self.api_base}search/s/{from_name}/{to_name}/{day}/{day}"
        try:
            return await ctx.http.post_json(
                url,
                params=dict(SEARCH_QUERY),
                json=SEARCH_BODY,
                headers={
                    "Accept": "application/json",
                    "Origin": self.booking_base.rstrip("/"),
                    "Referer": self.booking_base,
                },
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

    # ---------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, list):
            raise ProviderError("risposta inattesa: attesa una lista di soluzioni")

        legs: list[Leg] = []
        for solution in raw:
            if not isinstance(solution, dict):
                continue
            leg = self._to_leg(solution, origin, destination, ctx)
            if leg is not None:
                legs.append(leg)
        return legs

    def _to_leg(
        self, solution: dict, origin: Node, destination: Node, ctx: SearchContext
    ) -> Leg | None:
        trips = [t for t in solution.get("trips") or [] if isinstance(t, dict)]
        if not trips:
            return None
        if any(t.get("canceled") or (t.get("trip") or {}).get("canceled") for t in trips):
            return None

        timezone = solution.get("timezone") or DEFAULT_TZ
        depart = _local(trips[0].get("departureDateTime"), timezone)
        arrive = _local(trips[-1].get("arrivalDateTime"), timezone)
        if depart is None or arrive is None or arrive <= depart:
            return None
        if depart.date() != ctx.date:
            return None

        stops = self._cached_stops
        from_node = _stop_node(stops, trips[0].get("departureStopId"), origin)
        to_node = _stop_node(stops, trips[-1].get("arrivalStopId"), destination)

        price = solution.get("calculatedPrice")
        if not isinstance(price, (int, float)) or price <= 0:
            price = solution.get("fullPrice")
        fare = None
        notes: list[str] = []
        if isinstance(price, (int, float)) and price > 0:
            fare = Fare(
                amount=round(float(price), 2),
                currency="EUR",
                refundable=False,
                included_cabin_bags=1,
                included_checked_bags=1,
            )
        else:
            notes.append("prezzo non esposto per questa corsa")

        segments = [
            f"{_stop_name(stops, t.get('departureStopId'))} {_hhmm(t.get('departureDateTime'))}"
            f" -> {_stop_name(stops, t.get('arrivalStopId'))} {_hhmm(t.get('arrivalDateTime'))}"
            for t in trips
        ]

        from app.models import node_distance_km

        distance_km = node_distance_km(from_node, to_node) * 1.35
        return Leg(
            provider=self.id,
            mode=self.mode,
            origin=from_node,
            destination=to_node,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=None,
            fare=fare,
            booking_url=self._booking_url(from_node, to_node, ctx),
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            internal_changes=max(0, len(trips) - 1),
            segments=segments,
            notes=notes,
        )

    #: Riempito da `search()` prima del parsing: il catalogo serve a dare nomi e
    #: coordinate alle fermate, che nella risposta compaiono solo come id.
    _cached_stops: dict[str, dict] = {}

    async def search(
        self, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        self._cached_stops = (await self._catalog(ctx))["stops"]
        return await super().search(origin, destination, ctx)

    def _booking_url(self, origin: Node, destination: Node, ctx: SearchContext) -> str:
        if not self.booking_base:
            return self.website
        stamp = ctx.date.strftime("%y%m%d")
        return (
            f"{self.booking_base}it/from/{city_key(origin).title()}/{stamp}"
            f"/to/{city_key(destination).title()}/"
        )


def _node_for_locality(catalog: dict, label: str, mode: Mode) -> Node | None:
    """Costruisce un nodo a partire da una localita' del catalogo operatore."""
    key = _normalize(label)
    canonical = catalog["localities"].get(key)
    if canonical is None:
        return None
    for stop_id, stop in catalog["stops"].items():
        if _normalize(stop.get("locality") or "") != key:
            continue
        if stop.get("lat") is None or stop.get("lon") is None:
            continue
        return Node(
            id=f"alb:{stop_id}",
            name=stop.get("name") or canonical,
            kind=NodeKind.PORT if mode is Mode.FERRY else NodeKind.BUS_STOP,
            lat=stop["lat"],
            lon=stop["lon"],
            country="IT",
            city=canonical,
            timezone=stop.get("tz") or DEFAULT_TZ,
        )
    return None


def _name_candidates(node: Node) -> list[str]:
    """Nomi con cui provare a riconoscere una localita', dal piu' specifico."""
    candidates: list[str] = []
    for raw in (node.city, city_key(node), node.name):
        key = _normalize(str(raw or ""))
        if not key or key in candidates:
            continue
        candidates.append(key)
        words = key.split()
        for size in (2, 1):
            if len(words) > size:
                shorter = " ".join(words[:size])
                if shorter not in candidates:
                    candidates.append(shorter)
    return candidates


def _local(value: Any, timezone: str) -> datetime | None:
    """Converte un orario Albatross nell'ora locale del fuso indicato.

    La piattaforma serializza gli orari in UTC (offset `+00:00`) e allega a
    ogni soluzione il fuso in cui vanno letti (`Europe/Rome`). L'offset e'
    vero, non decorativo: verificato contro il sito di Marino, dove il
    notturno Torino-Matera del 14 agosto parte alle 18:30 e l'API riporta
    16:30Z. Senza la conversione ogni corsa risulterebbe due ore prima, e il
    riconoscimento dei viaggi notturni e la penalita' sull'ora di arrivo
    sarebbero calcolati sull'orario sbagliato.

    Se un giorno arrivasse un orario senza offset, va letto come ora locale:
    e' l'unica interpretazione sensata quando il fuso e' dichiarato a parte."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    try:
        zone = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - fuso sconosciuto: si ripiega sull'Italia
        zone = ZoneInfo(DEFAULT_TZ)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _hhmm(value: Any) -> str:
    if not isinstance(value, str):
        return "?"
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return "?"


def _stop_name(stops: dict, stop_id: Any) -> str:
    entry = stops.get(str(stop_id)) if stop_id else None
    return (entry or {}).get("name") or "?"


def _stop_node(stops: dict, stop_id: Any, fallback: Node) -> Node:
    entry = stops.get(str(stop_id)) if stop_id else None
    if not entry:
        return fallback
    lat, lon = entry.get("lat"), entry.get("lon")
    if lat is None or lon is None:
        return fallback.model_copy(
            update={"id": f"alb:{stop_id}", "name": entry.get("name") or fallback.name}
        )
    return Node(
        id=f"alb:{stop_id}",
        name=entry.get("name") or fallback.name,
        kind=NodeKind.BUS_STOP,
        lat=lat,
        lon=lon,
        country=fallback.country or "IT",
        city=entry.get("locality") or fallback.city,
        timezone=entry.get("tz") or DEFAULT_TZ,
    )


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Il catalogo contiene fermate senza coordinate, salvate come zero.
    return number if number else None


# --------------------------------------------------------------- operatori

#: Giorno in cui le tratte di prova qui sotto sono state viste funzionare.
#: Molti di questi operatori hanno una corsa al giorno o servizi stagionali:
#: verificarli in una data qualunque li farebbe risultare rotti quando invece
#: quel giorno non e' semplicemente giorno di servizio.
SAMPLE_DATE = "2026-08-14"

#: Gli operatori della piattaforma, uno per riga.
#:
#:   (id, nome, host API, sito di prenotazione, sito pubblico, modo, tratta di
#:    prova[, giorno della prova])
#:
#: L'ultimo campo e' facoltativo e serve a chi non circola nel giorno comune:
#: senza, vale `SAMPLE_DATE`.
#:
#: La tratta di prova non e' decorativa: `scripts/check_providers.py` la usa per
#: accorgersi che un parser si e' rotto, quindi deve essere una coppia che
#: quell'operatore serve davvero. Tutte quelle qui sotto sono state verificate
#: con una ricerca reale: zero corse li' significa guasto, non giornata vuota.
#:
#: Per trovare altri operatori della piattaforma c'e'
#: `scripts/probe_albatross.py`: parte dai `/Carriers` degli host gia' noti,
#: ne ricava i domini, cerca l'installazione (`booking.<dominio>/apiUrl.js`) e
#: verifica una tratta con corse vere leggendo le linee da `/Lines`. Il bundle
#: del motore di prenotazione non elenca piu' gli host degli altri operatori:
#: dalla versione 8.4 ciascuna installazione legge il proprio `serverUrl` da
#: `apiUrl.js`, quindi quella strada e' chiusa.
OPERATORS: tuple[tuple, ...] = (
    # --- lunga percorrenza, dorsale nord-sud ---
    ("marino", "Marino Autolinee", "https://api.marinobus.it/",
     "https://booking.marinobus.it/", "https://marinobus.it/", Mode.BUS,
     ("Torino", "Altamura")),
    ("marozzi", "Marozzi", "https://albatrossapi.marozzivt.it/",
     "https://shop.marozzivt.it/", "https://www.marozzivt.it/", Mode.BUS,
     ("Sorrento", "Roma")),
    ("federico", "Autolinee Federico", "https://api.autolineefederico.it/",
     "", "https://www.autolineefederico.it/", Mode.BUS,
     ("Serravalle Scrivia", "Marina di Gioiosa Ionica")),
    ("liscio", "Autolinee Liscio", "https://albatrossapi.autolineeliscio.it/",
     "", "https://www.autolineeliscio.it/", Mode.BUS, ("Roma", "Matera")),
    ("intersaj", "InterSAJ", "https://api.intersajticket.it/",
     "", "https://www.intersajticket.it/", Mode.BUS, ("Rende", "Brindisi")),
    ("gargano", "Ferrovie del Gargano", "https://api.ferroviedelgargano.com/",
     "", "https://www.ferroviedelgargano.com/", Mode.BUS,
     ("Settimo Torinese", "Monte Sant'Angelo")),
    ("satam", "SATAM", "https://api.satambus.it/", "",
     "https://www.satambus.it/", Mode.BUS, ("Genova", "Francavilla al Mare")),
    ("prontobus", "Pronto Bus Italia", "https://albatrossapi.prontobusitalia.it/",
     "", "https://www.prontobusitalia.it/", Mode.BUS,
     ("Torre de' Passeri", "Pratola Peligna")),

    # --- Sicilia ---
    ("sais", "SAIS Autolinee", "https://api.saisautolinee.it/", "",
     "https://www.saisautolinee.it/", Mode.BUS, ("Gela", "Modica")),
    ("salemi", "Autoservizi Salemi", "https://api.autoservizisalemi.it/", "",
     "https://www.autoservizisalemi.it/", Mode.BUS, ("Petrosino", "Salemi")),
    ("giuntabus", "Giuntabus", "https://api.giuntabus.com/", "",
     "https://www.giuntabus.com/", Mode.BUS, ("Messina", "Milazzo")),
    ("magtour", "Magtour", "https://api.magtour.it/", "",
     "https://www.magtour.it/", Mode.BUS, ("Tortorici", "Messina")),
    ("prestia", "Prestia e Comande", "https://api.prestiaecomande.it/", "",
     "https://www.prestiaecomande.it/", Mode.BUS, ("Palermo", "Cianciana")),
    ("onebus", "OneBus", "https://api.onebus.it/", "",
     "https://www.onebus.it/", Mode.BUS, ("Tarsia", "Raffadali")),

    # --- centro e sud ---
    ("consorzio", "Consorzio Autolinee", "https://api.consorzioautolinee.it/", "",
     "https://www.consorzioautolinee.it/", Mode.BUS, ("Sala Consilina", "San Lucido")),
    ("dimaio", "Gruppo Di Maio", "https://api.gruppodimaio.it/", "",
     "https://www.gruppodimaio.it/", Mode.BUS, ("Calitri", "Bisaccia Nuova")),
    ("tiemme", "Tiemme", "https://api.tiemmespa.it/", "",
     "https://www.tiemmespa.it/", Mode.BUS, ("Poggibonsi", "Grosseto")),

    # --- collegamenti con gli aeroporti ---
    ("fiumicinoexpress", "Fiumicino Express", "https://api.fiumicinoexpress.com/", "",
     "https://www.fiumicinoexpress.com/", Mode.BUS, ("Napoli", "Fiumicino aeroporto")),
    ("bus4fly", "Bus4Fly", "https://api.bus4fly.com/", "",
     "https://www.bus4fly.com/", Mode.BUS, ("Firenze", "Pisa Aeroporto ")),

    # --- montagna ---
    ("cortina", "Cortina Express", "https://api.cortinaexpress.it/", "",
     "https://www.cortinaexpress.it/", Mode.BUS, ("Costadedoi", "Dobbiaco")),
    ("livigno", "Livigno Express", "https://api.livignoexpress.com/", "",
     "https://www.livignoexpress.com/", Mode.BUS, ("Livigno", "Trenino Rosso")),
    ("altabadia", "Alta Badia Bus", "https://api.altabadiabus.eu/", "",
     "https://www.altabadiabus.eu/", Mode.BUS, ("La Villa", "Badia")),

    # --- regionali. Coprono tratte che nessun altro fa, e costano quasi nulla:
    #     se la localita' non e' nel loro catalogo l'adapter esce subito, senza
    #     nemmeno una richiesta di rete, perche' il catalogo e' in cache ---
    ("frecciaroma", "Freccia Roma", "https://api.frecciaroma.it/", "",
     "https://www.frecciaroma.it/", Mode.BUS, ("Fermo", "Tiburtina")),
    ("acierno", "Acierno", "https://api.acierno.com/", "",
     "https://www.acierno.com/", Mode.BUS, ("Sirignano", "Altavilla Irpina")),
    ("barzi", "Barzi Service", "https://api.barziservice.com/", "",
     "https://www.barziservice.com/", Mode.BUS, ("Vacil", "Treviso")),
    ("sbernaviaggi", "Sberna Viaggi", "https://api.sbernaviaggi.it/", "",
     "https://www.sbernaviaggi.it/", Mode.BUS,
     ("Sant'Agata di Militello", "Caronia")),
    ("sommatinese", "Autolinee Sommatinese", "https://api.sommatinese.it/", "",
     "https://www.sommatinese.it/", Mode.BUS, ("Lascari", "Mazzaforno")),
    ("gaspari", "Gaspari Lines", "https://api.gasparilines.it/", "",
     "https://www.gasparilines.it/", Mode.BUS,
     ("Montegualtieri", "Guardia Vomano")),
    ("giuntatrasporti", "Giuntabus Trasporti", "https://api.giuntabustrasporti.com/", "",
     "https://www.giuntabustrasporti.com/", Mode.BUS, ("Milazzo", "Olivarella")),
    ("dipaola", "Autonoleggio Di Paola", "https://api.autonoleggiodipaola.com/", "",
     "https://www.autonoleggiodipaola.com/", Mode.BUS,
     ("Catania Aeroporto", "Patti")),
    # Collegamenti col mare di Marino, venduti su un'installazione a parte:
    # circolano solo nei fine settimana d'estate, da qui la data propria.
    ("marinourbano", "MarinoBus Urbano", "https://api.marinobusurbano.it/",
     "https://booking.marinobusurbano.it/", "https://www.marinobusurbano.it/",
     Mode.BUS, ("Altamura", "Castellaneta Marina"), "2026-08-15"),

    # --- traghetti: la stessa piattaforma, e finalmente il modo `nave` ha
    #     qualcuno dietro ---
    ("libertylines", "Liberty Lines", "https://api.libertylines.it/", "",
     "https://www.libertylines.it/", Mode.FERRY, ("Marettimo", "Favignana")),
    ("blunavy", "Blu Navy", "https://api.blunavytraghetti.com/", "",
     "https://www.blunavytraghetti.com/", Mode.FERRY, ("Portoferraio", "Piombino")),
    ("ichnusa", "Ichnusa Lines", "https://api.ichnusalines.com/", "",
     "https://www.ichnusalines.com/", Mode.FERRY, ("Santa Teresa Gallura", "Bonifacio")),
)


def _build_providers() -> None:
    """Crea e registra una classe per ciascuna riga della tabella.

    Scriverle a mano sarebbe trenta volte lo stesso blocco: la tabella sopra e'
    l'artefatto da leggere, queste classi solo la sua conseguenza."""
    for row in OPERATORS:
        provider_id, name, api, booking, site, mode, sample = row[:7]
        sample_day = row[7] if len(row) > 7 else SAMPLE_DATE
        register(
            type(
                f"Albatross_{provider_id}",
                (AlbatrossProvider,),
                {
                    "id": provider_id,
                    "name": name,
                    "mode": mode,
                    "api_base": api,
                    "booking_base": booking,
                    "website": site,
                    "sample_route": sample,
                    "sample_date": sample_day,
                    "__doc__": f"{name}, sulla piattaforma Albatross.",
                },
            )
        )


_build_providers()
