"""Transitous, il servizio comunitario di orari del trasporto pubblico.

E' la risposta a un buco che il progetto aveva e non dichiarava: il trasporto
locale. Una ricerca **Asti -> Canelli** — ventun chilometri, l'astigiano — non
aveva nessun operatore in grado di rispondere. Trenitalia dice il vero quando
risponde "nessuna soluzione" (verificato il 2026-08-22 sul suo backend: quella
tratta in treno non esiste), FlixBus non ci passa, e l'unico mezzo pubblico
reale e' un autobus di linea che nessun adapter conosceva. Transitous quella
corsa la sa: **linea 41, cinquantadue minuti, zero cambi**.

Sotto c'e' MOTIS, un motore libero, alimentato dai GTFS aperti che gli enti
pubblicano. Nessuna chiave, nessun account:

  GET api.transitous.org/api/v1/plan?fromPlace=lat,lon&toPlace=lat,lon&time=...

Non e' un operatore, e' un pianificatore. Da qui tre conseguenze scritte nel
codice piu' sotto: niente prezzi, orari in UTC da convertire, e soprattutto una
**disciplina di richiesta**, perche' dietro non c'e' un'azienda ma dei
volontari che dichiarano il routing "resource-intensive" e chiedono di essere
contattati prima che qualcuno cominci a fare molte richieste.

Le condizioni d'uso (https://transitous.org/api/) sono impegni, non consigli:
solo applicazioni open source (Trip Finder e' MIT), niente uso commerciale, un
`User-Agent` che dica chi siamo e come contattarci, e l'attribuzione visibile
delle fonti — che sta nel piede della pagina, insieme a quella di OpenStreetMap.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.models import Leg, Mode, Node, NodeKind, node_distance_km
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.http_client import HttpError
from app.providers.registry import register
from app.version import VERSION

logger = logging.getLogger(__name__)

PLAN_URL = "https://api.transitous.org/api/v1/plan"
SITE_URL = "https://transitous.org/"
PROGETTO_URL = "https://github.com/DiegoRiccardi1234/trip-finder"

#: Chi siamo e come contattarci, come chiede la politica d'uso: nome, versione,
#: un recapito. E' l'URL del repository e non un indirizzo di posta: serve a
#: poterci scrivere per coordinare un cambio rompente, e le issue pubbliche
#: bastano a quello senza spedire il recapito personale di qualcuno dentro
#: l'intestazione di ogni richiesta.
USER_AGENT = f"TripFinder/{VERSION} (+{PROGETTO_URL})"

#: Sopra questa distanza non si chiede. Non e' prudenza generica: verificato il
#: 2026-08-22, Torino -> Matera risponde `200` con **zero itinerari**, perche'
#: i feed aperti coprono il locale e non cuciono la lunga percorrenza. Chiedere
#: comunque vorrebbe dire spendere il calcolo piu' caro che hanno per ricevere
#: una lista vuota. Asti -> Canelli sono ventun chilometri.
MAX_TRATTA_KM = 150.0

#: Quanti itinerari chiedere. Tre coprono la mattina e il primo pomeriggio di
#: una linea locale senza far diventare il conto una giornata intera.
ITINERARI = 3

#: I modi che questo adapter ha il diritto di restituire. Si dichiara pullman e
#: deve restituire pullman: il ranker non rifiltra per modo, quindi un treno
#: consegnato sotto la bandiera del bus scavalcherebbe in silenzio la scelta di
#: chi ha tolto la spunta al treno. Meglio perdere un itinerario che mentire su
#: che cosa e'.
MODI_AMMESSI = {"BUS", "COACH"}

#: Cio' che non e' una corsa: i modi con cui ci si sposta da soli.
SENZA_ORARIO = {"WALK", "BIKE", "CAR", "ODM", "RENTAL"}

#: Autobus di linea, per passeggero-km. Piu' alto dei 0,029 del pullman a
#: lunga percorrenza: un mezzo extraurbano fa fermate, viaggia meno pieno e non
#: tiene la velocita' di crociera.
CO2_KG_PER_KM = 0.068


@register
class Transitous(Provider):
    id = "transitous"
    name = "Transitous"
    mode = Mode.BUS
    tier = 1
    #: Come OBB e SBB: orari senza prezzo. `routing/cost.py` stima e
    #: **dichiara** la stima, che e' l'unica cosa onesta da fare con un
    #: biglietto che si compra a bordo.
    has_prices = False
    #: Per citta': collassa le due fermate per lato in una richiesta sola. Non
    #: e' del tutto esatto — MOTIS pianifica da coordinate, non da citta' — ma
    #: e' il vocabolario che il motore ha gia', e vale un fattore quattro sul
    #: numero di richieste verso un servizio di volontari.
    granularity = "city"
    #: Sei ore invece dei quindici minuti delle tariffe: un orario non si muove
    #: durante la giornata, e ogni risposta riusata e' una richiesta in meno.
    cache_kind = "schedule"
    #: Nessun elenco di paesi: i feed sono comunitari e crescono. Dichiarare una
    #: lista significherebbe escludere in silenzio le regioni aggiunte domani.
    countries = None
    website = SITE_URL
    sample_route = ("Asti", "Canelli")

    def supports_node(self, node: Node) -> bool:
        # Si pianifica da coordinate: non serve nessun identificatore nostro, e
        # chiederlo escluderebbe proprio le fermate minori che sono il motivo
        # per cui questo adapter esiste.
        return node.kind in {NodeKind.BUS_STOP, NodeKind.STATION, NodeKind.CITY}

    def can_serve(self, origin: Node, destination: Node) -> bool:
        if node_distance_km(origin, destination) > MAX_TRATTA_KM:
            return False
        return super().can_serve(origin, destination)

    def cache_key(self, origin: Node, destination: Node, ctx: SearchContext) -> str:
        # L'ora minima entra nella richiesta, quindi deve entrare nella chiave:
        # altrimenti una ricerca col vincolo si riprende la risposta del
        # mattino gia' salvata, e il vincolo sparisce senza lasciare traccia.
        ora = ctx.depart_after.isoformat() if ctx.depart_after else "-"
        return f"{super().cache_key(origin, destination, ctx)}:{ora}"

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        # **La disciplina di richiesta, e sta qui perche' qui e' dove si
        # spende.** Solo la tratta chiesta: sulle coincidenze intermedie questo
        # adapter tace. Con questo, `granularity="city"` e la cache a sei ore,
        # una ricerca costa **una** richiesta a Transitous invece di una per
        # coppia di fermate, che e' esattamente cio' che la loro politica
        # chiede di non fare.
        #
        # La condizione e' doppia di proposito: `is_endpoint_pair` risponde
        # `False` anche quando gli estremi non sono stati dichiarati affatto, e
        # succede negli script di diagnostica. Senza il primo termine,
        # `check_providers.py` vedrebbe un adapter perennemente muto e lo
        # direbbe rotto.
        if ctx.endpoints_noti and not ctx.is_endpoint_pair(origin, destination):
            raise NotServed("Transitous risponde solo sulla tratta cercata")

        partenza = ctx.local_dt(origin, ctx.date, ctx.depart_after or time(0, 1))
        params = {
            "fromPlace": f"{origin.lat},{origin.lon}",
            "toPlace": f"{destination.lat},{destination.lon}",
            "time": partenza.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "numItineraries": ITINERARI,
        }
        try:
            payload = await ctx.http.get_json(
                PLAN_URL,
                params=params,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

        if not isinstance(payload, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")
        return payload

    # ---------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")
        itinerari = raw.get("itineraries")
        if itinerari is None:
            raise ProviderError("risposta senza il blocco degli itinerari")

        # Si ignora `raw["direct"]`: sono i percorsi a piedi soltanto. Veri, ma
        # non sono un collegamento da mettere in classifica accanto a una corsa.
        legs: list[Leg] = []
        for itinerario in itinerari:
            if not isinstance(itinerario, dict):
                continue
            leg = self._to_leg(itinerario, origin, destination, ctx)
            if leg is not None:
                legs.append(leg)
        legs.sort(key=lambda item: item.depart)
        return legs

    def _to_leg(
        self, itinerario: dict, origin: Node, destination: Node, ctx: SearchContext
    ) -> Leg | None:
        gambe = [g for g in itinerario.get("legs") or [] if isinstance(g, dict)]
        trasporto = [
            g for g in gambe if str(g.get("mode") or "").upper() not in SENZA_ORARIO
        ]
        if not trasporto:
            return None

        modi = {str(g.get("mode") or "").upper() for g in trasporto}
        if not modi <= MODI_AMMESSI:
            logger.debug("%s: scarto un itinerario %s, non e' pullman", self.id, modi)
            return None

        prima, ultima = trasporto[0], trasporto[-1]
        depart = _momento(prima.get("startTime"), (prima.get("from") or {}).get("tz"))
        arrive = _momento(ultima.get("endTime"), (ultima.get("to") or {}).get("tz"))
        if depart is None or arrive is None or arrive <= depart:
            return None
        if depart.date() != ctx.date:
            return None

        # Gli estremi sono le fermate del **trasporto**, non l'inizio e la fine
        # della camminata: il primo e l'ultimo miglio li calcola gia'
        # `routing/feasibility.py` col suo modello, e sommarne due diversi
        # sarebbe peggio che sceglierne uno. La camminata resta scritta nei
        # segmenti, dove si legge senza entrare nel conto.
        da = _fermata(prima.get("from"), origin)
        a = _fermata(ultima.get("to"), destination)
        if da is None or a is None:
            return None

        segmenti: list[str] = []
        for gamba in gambe:
            modo = str(gamba.get("mode") or "").upper()
            inizio = (gamba.get("from") or {}).get("name") or "?"
            fine = (gamba.get("to") or {}).get("name") or "?"
            if modo in SENZA_ORARIO:
                minuti = int((gamba.get("duration") or 0) // 60)
                if minuti:
                    segmenti.append(f"a piedi {minuti} min: {inizio} -> {fine}")
                continue
            linea = gamba.get("routeShortName") or gamba.get("routeLongName") or ""
            segmenti.append(f"{linea} {inizio} -> {fine}".strip())

        vettori = {str(g.get("agencyName")) for g in trasporto if g.get("agencyName")}
        linee = [str(g.get("routeShortName")) for g in trasporto if g.get("routeShortName")]

        notes = [
            "orario da Transitous, che raccoglie i dati aperti degli operatori "
            "locali: il prezzo non e' pubblicato e il biglietto si compra "
            "dall'operatore o a bordo",
        ]
        cambi = int(itinerario.get("transfers") or 0)
        if cambi:
            quanti = "un cambio" if cambi == 1 else f"{cambi} cambi"
            notes.append(f"{quanti} dentro lo stesso viaggio")

        distanza = node_distance_km(da, a) * 1.35  # strade, non linea d'aria
        return Leg(
            provider=self.id,
            mode=Mode.BUS,
            origin=da,
            destination=a,
            depart=depart,
            arrive=arrive,
            operator=", ".join(sorted(vettori)) or "trasporto pubblico locale",
            vehicle=" + ".join(linee[:3]) or None,
            fare=None,
            booking_url=SITE_URL,
            co2_kg=round(distanza * CO2_KG_PER_KM, 1) if distanza else None,
            internal_changes=cambi,
            segments=segmenti[:8],
            notes=notes,
        )


def _momento(value: Any, fuso: Any) -> datetime | None:
    """Gli orari arrivano in UTC con la `Z`, il fuso della fermata a parte.

    Vanno **convertiti**, non reinterpretati: e' la stessa trappola gia' pagata
    con Albatross, dove leggerli come locali spostava ogni corsa di due ore e
    faceva inventare viaggi notturni che non esistevano."""
    if not isinstance(value, str) or not value:
        return None
    try:
        istante = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if istante.tzinfo is None:
        return None
    if isinstance(fuso, str) and fuso:
        try:
            return istante.astimezone(ZoneInfo(fuso))
        except Exception:  # noqa: BLE001 - un fuso ignoto non vale una ricerca persa
            logger.debug("fuso non riconosciuto: %r", fuso)
    return istante


def _fermata(posto: Any, ripiego: Node) -> Node | None:
    """Il nodo della fermata, costruito dalla risposta.

    Qui le coordinate ci sono davvero, e questo adapter non ha quindi il
    problema che ha FlixBus: nessun nodo puo' portare il nome di un posto e la
    posizione di un altro."""
    if not isinstance(posto, dict):
        return None
    lat, lon = posto.get("lat"), posto.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    identificativo = posto.get("stopId") or f"{lat:.5f},{lon:.5f}"
    return Node(
        id=f"tr:{identificativo}",
        name=str(posto.get("name") or ripiego.name),
        kind=NodeKind.BUS_STOP,
        lat=float(lat),
        lon=float(lon),
        country=ripiego.country,
        city=ripiego.city,
        timezone=posto.get("tz") or ripiego.timezone,
    )
