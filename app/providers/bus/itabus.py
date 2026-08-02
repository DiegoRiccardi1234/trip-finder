"""Itabus, tramite l'API pubblica del sito.

Due endpoint, entrambi aperti e senza autenticazione:

  - `Api-Stations` restituisce l'intero elenco fermate con coordinate, citta' di
    appartenenza e **destinazioni raggiungibili**. Si scarica una volta al mese:
    da qui esce sia la mappatura dei nomi sia la copertura, che permette di
    scartare a costo zero le tratte che Itabus non fa.
  - `Api-Travels` restituisce le corse di un giorno con orari, prezzi e servizi
    a bordo.

Un dettaglio che vale copertura: accanto alle fermate esistono codici di
**citta'** con suffisso `_T` (`TOR_T`, `MAT_T`) che aggregano tutti i capolinea
del posto. Cercare per citta' invece che per singola fermata trova tutte le
combinazioni in una richiesta sola: Milano come citta' ha 307 destinazioni,
la sola Lampugnano molte meno.

Attenzione al percorso: la parte locale dell'URL e' `/it/`, non `/it_IT/`.
Con la seconda il server risponde 500 senza spiegare perche'.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from app.models import Fare, Leg, Mode, Node, NodeKind
from app.orchestrator import cache
from app.providers import coverage
from app.providers.base import NotServed, Provider, ProviderError, SearchContext, city_key
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

BASE = "https://www.itabus.it/on/demandware.store/Sites-ITABUS-Site/it"
STATIONS_URL = f"{BASE}/Api-Stations"
TRAVELS_URL = f"{BASE}/Api-Travels"
SHOP_URL = "https://www.itabus.it/it/search"

CO2_KG_PER_KM = 0.029  # pullman a lunga percorrenza, per passeggero-km


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return " ".join(cleaned.split())


@register
class Itabus(Provider):
    id = "itabus"
    name = "Itabus"
    mode = Mode.BUS
    tier = 1
    granularity = "city"
    countries = frozenset({"IT"})
    website = "https://www.itabus.it/"
    sample_route = ("Milano", "Roma")

    def supports_node(self, node: Node) -> bool:
        return node.kind in {NodeKind.BUS_STOP, NodeKind.STATION, NodeKind.CITY}

    def can_serve(self, origin: Node, destination: Node) -> bool:
        # Vedi `albatross.py`: si aggiunge una condizione a quella della base,
        # non la si sostituisce, altrimenti `countries` non filtra piu' nulla.
        if city_key(origin) == city_key(destination):
            return False
        return super().can_serve(origin, destination)

    # ------------------------------------------------------------- anagrafica

    async def _stations(self, ctx: SearchContext) -> dict[str, Any]:
        """Indice delle fermate, in cache per un mese.

        L'endpoint pubblica `code`, `name`, `synonyms` e `parent`. Non porta ne'
        coordinate ne' l'elenco delle destinazioni (il campo `destinations`
        esiste ma arriva sempre vuoto): quindi da qui esce solo la traduzione
        nome-codice, e la copertura non e' ricavabile. Meglio saperlo che
        dedurre dal vuoto che Itabus non serva nulla."""
        cached = await cache.get("itabus:index")
        if cached is not None:
            return cached

        try:
            payload = await ctx.http.get_json(
                STATIONS_URL, headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(f"elenco fermate non disponibile: {exc}") from exc

        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ProviderError("elenco fermate vuoto o in formato inatteso")

        by_code: dict[str, dict] = {}
        children: dict[str, int] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("code"):
                continue
            code = str(entry["code"])
            parent = str(entry.get("parent") or "")
            by_code[code] = {
                "name": entry.get("name") or code,
                "parent": parent,
                "synonyms": entry.get("synonyms") or [],
            }
            if parent:
                children[parent] = children.get(parent, 0) + 1

        # Per ogni nome si tiene il candidato migliore, non l'ultimo visto: piu'
        # fermate condividono gli stessi sinonimi e senza un criterio esplicito
        # "Roma" finiva su un capolinea secondario invece che sul codice che
        # aggrega tutta la citta'. Vince chi e' un codice di citta' (suffisso
        # `_T`) e, a parita', chi ha piu' capolinea sotto di se'.
        best_by_name: dict[str, tuple[tuple[int, int], str]] = {}
        for code, entry in by_code.items():
            rank = (1 if code.endswith("_T") else 0, children.get(code, 0))
            for label in [entry["name"], *entry["synonyms"]]:
                key = _normalize(str(label or ""))
                if not key:
                    continue
                current = best_by_name.get(key)
                if current is None or rank > current[0]:
                    best_by_name[key] = (rank, code)

        index = {
            "by_code": by_code,
            "by_name": {key: code for key, (_, code) in best_by_name.items()},
        }
        await cache.set("itabus:index", index, kind="resolve")
        logger.debug("Itabus: %d fermate indicizzate", len(by_code))
        return index

    async def _code_for(self, node: Node, ctx: SearchContext) -> str:
        declared = self.native_id(node)
        if declared:
            return declared

        index = await self._stations(ctx)
        by_name = index["by_name"]
        for key in _name_candidates(node):
            if key in by_name:
                return by_name[key]
        raise NotServed(f"Itabus non ha fermate a {city_key(node)!r}")

    # ----------------------------------------------------------------- ricerca

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        origin_code = await self._code_for(origin, ctx)
        dest_code = await self._code_for(destination, ctx)
        if origin_code == dest_code:
            raise NotServed("origine e destinazione coincidono per Itabus")

        # Nessun filtro di copertura: l'operatore non pubblica le proprie rotte.
        # La richiesta di ricerca costa poche centinaia di millisecondi, quindi
        # e' preferibile provare che escludere una tratta per un'informazione
        # che non abbiamo.
        params = {
            "origin": origin_code,
            "destination": dest_code,
            "datestart": ctx.date.isoformat(),
            "adults": max(1, ctx.pax),
            "children": 0,
            "membership": "false",
            "code": "",
        }
        try:
            return await ctx.http.get_json(
                TRAVELS_URL, params=params, headers={"Accept": "application/json"}
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

    # --------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")
        if raw.get("error"):
            raise ProviderError(str(raw["error"])[:200])

        outbound = ((raw.get("data") or {}).get("outbound")) or {}
        routes = outbound.get("routes")
        if routes is None:
            raise ProviderError("risposta senza il blocco delle corse")

        legs: list[Leg] = []
        for route in routes:
            if not isinstance(route, dict):
                continue
            leg = self._to_leg(route, origin, destination, ctx)
            if leg is not None:
                legs.append(leg)
        return legs

    def _to_leg(
        self, route: dict, origin: Node, destination: Node, ctx: SearchContext
    ) -> Leg | None:
        if route.get("available") is False:
            return None
        depart = _parse_dt(route.get("departure_timestamp"))
        arrive = _parse_dt(route.get("arrival_timestamp"))
        if depart is None or arrive is None or arrive <= depart:
            return None
        if depart.date() != ctx.date:
            return None

        from_node = _stop_node(route.get("origin"), origin)
        to_node = _stop_node(route.get("destination"), destination)

        price = _offer_total(route.get("rates"))
        notes: list[str] = []
        fare: Fare | None = None
        if price is not None:
            fare = Fare(
                amount=round(price, 2),
                currency="EUR",
                fare_class="Economy",
                refundable=False,
                included_cabin_bags=1,
                included_checked_bags=1,
            )
        else:
            notes.append("prezzo non esposto per questa corsa")

        comforts = [
            str(service.get("description"))
            for service in route.get("services") or []
            if isinstance(service, dict) and service.get("description")
        ]
        if comforts:
            notes.append("a bordo: " + ", ".join(comforts[:5]))

        from app.models import node_distance_km

        distance_km = node_distance_km(from_node, to_node) * 1.35
        return Leg(
            provider=self.id,
            mode=Mode.BUS,
            origin=from_node,
            destination=to_node,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=route.get("service_name"),
            fare=fare,
            booking_url=_shop_url(route, ctx),
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            internal_changes=max(0, int(route.get("transferNumber") or 0)),
            segments=_segments(route),
            notes=notes,
        )


def _name_candidates(node: Node) -> list[str]:
    """Nomi con cui provare a riconoscere una fermata, dal piu' specifico.

    I nomi di stazione cominciano quasi sempre con la citta' ("Milano Centrale",
    "Torino Porta Nuova"): accorciandoli si arriva al nome della citta', che e'
    quello con cui Itabus indicizza i suoi capolinea. Si prova prima con due
    parole che con una, altrimenti "Reggio Emilia" finirebbe a Reggio Calabria."""
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


def _stop_node(payload: Any, fallback: Node) -> Node:
    """Nodo del capolinea realmente servito.

    Il capolinea di Itabus a Torino e' il Terminal di corso Vittorio Emanuele,
    non Porta Nuova, e va detto. Le coordinate pero' l'operatore non le
    pubblica: si tiene la posizione della localita' richiesta, che sbaglia al
    massimo di qualche chilometro dentro la stessa citta' e non falsa il
    confronto fra le soluzioni."""
    if not isinstance(payload, dict):
        return fallback
    code = payload.get("code")
    name = payload.get("name")
    if not code:
        return fallback
    return fallback.model_copy(
        update={
            "id": f"itabus:{code}",
            "name": str(name or fallback.name),
            "provider_ids": {**fallback.provider_ids, "itabus": str(code)},
        }
    )


#: Chiavi sotto cui l'offerta e' scomposta tratta per tratta. Sono il dettaglio
#: di un totale che sta gia' un livello sopra, mai un'offerta a se': entrarci
#: significa mettere il prezzo di una tratta a confronto con quello di un
#: viaggio intero.
BREAKDOWN_KEYS = frozenset({"items", "passenger_fares"})


def _offer_total(rates: Any) -> float | None:
    """Il piu' basso fra i totali delle offerte, per l'intero viaggio.

    L'albero e' annidato per operatore, famiglia e classe
    (`rates.ITABUS.FLEX.COMFORT`) ma la profondita' non e' fissa: certe
    famiglie esistono senza classi sotto e senza prezzo. Si scende quindi
    ricorsivamente, **potando su `items` e `passenger_fares`** — e quella
    potatura e' la sostanza della funzione, non un dettaglio di efficienza.

    Senza, il minimo cadeva sul segmento piu' economico invece che sul totale:
    Milano-Roma via Bologna risultava **24,99 invece di 64,98** (24,99 +
    39,99), e Torino-Catania 19,99 invece di 59,98. Il ranker penalizza un
    cambio di appena 0.04, quindi una corsa dimezzata cosi' scavalcava
    sistematicamente ogni diretta.

    E' lo stesso livello a cui leggono gli altri adapter, che infatti non
    hanno il problema: `calculatedPrice` in albatross, `price.amount` in
    trenitalia, `total_with_platform_fee` in flixbus. Nessuno scende sotto
    l'offerta.

    Se nessun totale e' leggibile si torna `None`: la gamba esce senza tariffa
    e `routing/cost.py` stima dichiarandolo, che e' meglio di un numero
    plausibile e sbagliato.
    """
    best: float | None = None

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, dict):
            value = node.get("price")
            if value is None:
                value = node.get("average_price")
            if isinstance(value, (int, float)) and value > 0:
                best = float(value) if best is None else min(best, float(value))
            for key, child in node.items():
                if key not in BREAKDOWN_KEYS:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(rates)
    return best


def _segments(route: dict) -> list[str]:
    """Le tratte interne, per il dettaglio in UI.

    Una corsa con cambio e' una gamba sola (biglietto unico, nessun rischio di
    coincidenza persa), ma aprendola si vedeva una riga sola come se fosse
    diretta. `route["legs"]` dice dove si cambia e a che ora."""
    rows: list[str] = []
    for leg in route.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        start = _parse_dt(leg.get("departure_timestamp"))
        end = _parse_dt(leg.get("arrival_timestamp"))
        if start is None or end is None:
            continue
        rows.append(
            f"{leg.get('origin')} {start:%H:%M} -> {leg.get('destination')} {end:%H:%M}"
        )
    return rows if len(rows) > 1 else []


def _shop_url(route: dict, ctx: SearchContext) -> str:
    origin = (route.get("origin") or {}).get("code", "")
    destination = (route.get("destination") or {}).get("code", "")
    params = {
        "origin": origin,
        "destination": destination,
        "datestart": ctx.date.isoformat(),
        "adults": max(1, ctx.pax),
    }
    return f"{SHOP_URL}?{urlencode(params)}"


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Itabus scrive il fuso come "+0200", senza i due punti.
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
