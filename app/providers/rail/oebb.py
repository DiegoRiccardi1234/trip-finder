"""OBB, le ferrovie austriache, tramite il backend HAFAS del loro orario.

`fahrplan.oebb.at/bin/mgate.exe` e' l'interfaccia che usa l'app ufficiale:
accetta un corpo JSON con una lista di richieste e non chiede altro che una
chiave di client, che il sito stesso pubblica. Due metodi bastano:

  LocMatch    dal nome di una localita' ai suoi identificatori
  TripSearch  le soluzioni fra due stazioni in una data

Perche' vale la pena: OBB vende i diretti Vienna-Venezia e i notturni verso
l'Italia, che nessun altro adapter del progetto conosce. Il dataset Trainline
porta gia' `obb_id` per le stazioni, quindi la traduzione dei nomi e' gratis e
`LocMatch` serve solo da rete di sicurezza.

Quello che **non** da': i prezzi. Il campo `trfRes` contiene solo un
collegamento al negozio, e il negozio e' dietro Cloudflare. Le gambe escono
senza tariffa, `routing/cost.py` la stima e la dichiara come stima. Meglio un
orario vero senza prezzo che nessun collegamento.

Due trappole del formato, entrambe verificate sulle risposte reali:

  - gli orari sono stringhe `HHMMSS` **locali**, con l'offset del fuso in
    minuti in un campo a parte (`dTZOffset`). Vanno composti, non interpretati
    come UTC;
  - quando una corsa scavalca la mezzanotte il campo diventa di **otto** cifre,
    `ddHHMMSS`, dove `dd` sono i giorni da aggiungere alla data di partenza.
    Letto come `HHMMSS` darebbe un orario impossibile e la corsa sparirebbe.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import Leg, Mode, Node, NodeKind
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

MGATE_URL = "https://fahrplan.oebb.at/bin/mgate.exe"
BOOKING_URL = "https://tickets.oebb.at/en/ticket"

#: Chiave di client pubblicata dall'app ufficiale. Senza, il servizio risponde
#: 200 ma con `err: PARSE`: non e' un segreto, e' il modo in cui HAFAS
#: distingue le applicazioni che lo interrogano.
CLIENT_AUTH = {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"}
CLIENT = {"id": "OEBB", "type": "IPH", "name": "oebbPROD-ADHOC", "v": "6030600"}
HAFAS_VERSION = "1.34"

#: Tutti i mezzi (maschera di bit HAFAS): treni a lunga percorrenza, regionali,
#: S-Bahn, bus e battelli inclusi.
ALL_PRODUCTS = "1023"

MAX_CHANGES = 3
RESULTS = 6

#: Emissioni per passeggero-km sul treno elettrico austriaco, che viaggia con
#: energia quasi interamente rinnovabile.
CO2_KG_PER_KM = 0.014

#: Paesi in cui OBB ha senso: l'Austria e i vicini che i suoi diretti servono.
#: Senza un vincolo, ogni tratta italiana con due stazioni che hanno un
#: `obb_id` finirebbe per interrogare Vienna senza motivo.
COUNTRIES = frozenset({"AT", "DE", "IT", "CH", "HU", "CZ", "SK", "SI", "HR"})


@register
class OeBB(Provider):
    id = "oebb"
    name = "OBB"
    mode = Mode.RAIL
    tier = 1
    countries = COUNTRIES
    has_prices = False
    website = BOOKING_URL
    #: Nomi locali: sono quelli con cui le stazioni compaiono nei dataset.
    #: Gli alias italiani ("Vienna", "Salisburgo") stanno in geo/overrides.json
    #: e servono a chi cerca, non al controllo di salute.
    sample_route = ("Wien", "Salzburg")

    def supports_node(self, node: Node) -> bool:
        return node.kind is NodeKind.STATION and bool(self.native_id(node))

    def can_serve(self, origin: Node, destination: Node) -> bool:
        """Almeno un capo in Austria.

        OBB compare nel dataset anche su stazioni italiane e tedesche, perche'
        le vende come destinazioni internazionali. Interrogarla per un
        Torino-Milano sarebbe pero' solo una richiesta in piu' su una tratta
        che Trenitalia copre meglio: il valore di questo adapter sta nei
        collegamenti che toccano l'Austria."""
        if not super().can_serve(origin, destination):
            return False
        return "AT" in {origin.country, destination.country}

    # ---------------------------------------------------------------- ricerca

    def _location(self, node: Node) -> dict:
        """Identificatore di stazione nella forma che HAFAS si aspetta."""
        return {
            "type": "S",
            "lid": f"A=1@O={node.name}@L={self.native_id(node)}@",
        }

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        if not self.native_id(origin) or not self.native_id(destination):
            raise NotServed("stazione senza identificatore OBB")

        body = {
            "ver": HAFAS_VERSION,
            "lang": "deu",
            "auth": CLIENT_AUTH,
            "client": CLIENT,
            "formatted": False,
            "svcReqL": [
                {
                    "meth": "TripSearch",
                    "id": "1|1|",
                    "req": {
                        "depLocL": [self._location(origin)],
                        "arrLocL": [self._location(destination)],
                        "outDate": ctx.date.strftime("%Y%m%d"),
                        "outTime": "000000",
                        "jnyFltrL": [
                            {"type": "PROD", "mode": "INC", "value": ALL_PRODUCTS}
                        ],
                        "getPasslist": False,
                        "getPolyline": False,
                        "maxChg": MAX_CHANGES,
                        "numF": RESULTS,
                    },
                }
            ],
        }
        try:
            payload = await ctx.http.post_json(
                MGATE_URL, json=body, headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

        if not isinstance(payload, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")
        if payload.get("err") not in (None, "OK"):
            raise ProviderError(f"HAFAS: {payload.get('err')} {payload.get('errTxt', '')}"[:180])
        return payload

    # ---------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")
        services = raw.get("svcResL") or []
        if not services or not isinstance(services[0], dict):
            raise ProviderError("risposta senza il blocco delle soluzioni")

        service = services[0]
        if service.get("err") not in (None, "OK"):
            # HAFAS usa lo stesso canale per "non ho trovato niente" e per gli
            # errori veri: il primo caso e' una risposta, non un guasto.
            if str(service.get("err")).upper() in {"H9360", "NO_MATCH", "LOCATION"}:
                raise NotServed(str(service.get("errTxt") or service.get("err")))
            raise ProviderError(f"{service.get('err')}: {service.get('errTxt', '')}"[:180])

        result = service.get("res") or {}
        common = result.get("common") or {}
        products = common.get("prodL") or []

        legs: list[Leg] = []
        for connection in result.get("outConL") or []:
            if not isinstance(connection, dict):
                continue
            leg = self._to_leg(connection, products, origin, destination, ctx)
            if leg is not None:
                legs.append(leg)
        legs.sort(key=lambda item: item.depart)
        return legs

    def _to_leg(
        self, connection: dict, products: list, origin: Node, destination: Node,
        ctx: SearchContext,
    ) -> Leg | None:
        base = _date_of(connection.get("date")) or ctx.date
        departure = connection.get("dep") or {}
        arrival = connection.get("arr") or {}

        depart = _moment(departure.get("dTimeS"), departure.get("dTZOffset"), base)
        arrive = _moment(arrival.get("aTimeS"), arrival.get("aTZOffset"), base)
        if depart is None or arrive is None or arrive <= depart:
            return None
        if depart.date() != ctx.date:
            return None

        sections = [s for s in connection.get("secL") or [] if isinstance(s, dict)]
        names: list[str] = []
        stops: list[str] = []
        for section in sections:
            journey = section.get("jny")
            if not isinstance(journey, dict):
                continue
            index = journey.get("prodX")
            if isinstance(index, int) and 0 <= index < len(products):
                label = str((products[index] or {}).get("name") or "").strip()
                if label and label not in names:
                    names.append(label)
            direction = str(journey.get("dirTxt") or "").strip()
            if direction:
                stops.append(f"{label if names else ''} -> {direction}".strip())

        changes = int(connection.get("chg") or 0)
        notes = ["prezzo non pubblicato dall'orario OBB: si acquista sul loro sito"]
        if changes:
            notes.append(f"{changes} cambi, biglietto unico OBB")

        from app.models import node_distance_km

        distance_km = node_distance_km(origin, destination)
        return Leg(
            provider=self.id,
            mode=Mode.RAIL,
            origin=origin,
            destination=destination,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=" + ".join(names[:3]) or None,
            fare=None,
            booking_url=BOOKING_URL,
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            internal_changes=changes,
            segments=stops[:6],
            notes=notes,
        )


def _date_of(value: Any) -> Any:
    if not isinstance(value, str) or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _moment(value: Any, offset_minutes: Any, base) -> datetime | None:
    """Compone un orario HAFAS nel suo fuso.

    Accetta sia `HHMMSS` sia `ddHHMMSS`, dove `dd` sono i giorni da sommare
    alla data della soluzione: e' cosi' che l'orario dichiara un arrivo del
    giorno dopo."""
    if not isinstance(value, str) or not value.isdigit():
        return None
    if len(value) == 8:
        days, clock = int(value[:2]), value[2:]
    elif len(value) == 6:
        days, clock = 0, value
    else:
        return None
    try:
        hour, minute, second = int(clock[:2]), int(clock[2:4]), int(clock[4:6])
    except ValueError:
        return None
    if hour > 23:
        # Alcune risposte usano le ore oltre le 24 per il giorno dopo.
        days += hour // 24
        hour = hour % 24

    minutes = offset_minutes if isinstance(offset_minutes, int) else 60
    zone = timezone(timedelta(minutes=minutes))
    try:
        moment = datetime(base.year, base.month, base.day, hour, minute, second, tzinfo=zone)
    except ValueError:
        return None
    return moment + timedelta(days=days)
