"""Trenitalia, tramite il backend del sito lefrecce.it.

Non e' un'API pubblicata: e' il BFF che serve il sito web. Risponde senza
autenticazione e senza cookie, il che la rende la sorgente ferroviaria italiana
piu' affidabile a disposizione. Va trattata come fragile: qualunque restyling
del sito puo' cambiarne la forma, per questo il parsing sta tutto in un punto
solo e i test girano su fixture salvate.

Una "solution" di Trenitalia e' un biglietto unico che puo' contenere piu' treni.
Qui diventa **una sola** gamba con `internal_changes` valorizzato: e' un solo
contratto, quindi non ha il rischio di coincidenza persa dei biglietti separati,
ma i cambi ci sono e vanno detti.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.models import Fare, Leg, Mode, Node
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.http_client import HttpError
from app.providers.registry import register

logger = logging.getLogger(__name__)

BASE = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website"
SOLUTIONS_URL = f"{BASE}/ticket/solutions"
LOCATIONS_URL = f"{BASE}/locations/search"
BOOKING_URL = "https://www.lefrecce.it/Channels.Website.WEB/"

#: Emissioni indicative per passeggero-km. Fonte: medie europee per il ferro.
CO2_KG_PER_KM = 0.035

#: Categorie che non vogliamo proporre come soluzione a se' stante.
URBAN_CATEGORIES = {"Urbano", "Metropolitano"}

#: Stato di una soluzione i cui posti sono finiti. Va guardato prima del
#: prezzo: vedi il commento in `_to_leg`.
SOLD_OUT = "SOLD_OUT"


#: Da che ora si guarda la giornata quando nessuno ha chiesto altro. Non le
#: 00:00 perche' il backend vuole un orario gia' cominciato.
PRIMA_ORA = "00:01:00.000"

#: Quante soluzioni tornano per richiesta. **Non e' una scelta nostra**: il
#: backend ne manda dieci qualunque cosa si chieda. `limit` viene accettato e
#: ignorato (30 e 100 rispondono le stesse dieci), e i nomi alternativi
#: plausibili — `pageSize`, `size`, `maxResults`, `numberOfSolutions` — sono
#: chiavi sconosciute e fanno HTTP 400. Verificato il 2026-08-17.
PAGINA = 10

#: Quante pagine al massimo. Sulla tratta piu' densa misurata, Firenze SMN ->
#: Torino Porta Nuova, la giornata intera stava in cinque pagine: 41 soluzioni,
#: dalle 00:40 alle 19:55, e la sesta tornava vuota.
MAX_PAGINE = 5

#: Quanto si aspettano, in secondi, le pagine dopo la prima. Non e' il budget
#: del provider (18 s per un `tier 1`): e' la fetta di quel budget che si e'
#: disposti a spendere per il **di piu'**. La prima pagina in coda costa fino a
#: ~7 s misurati; sei secondi di margine tengono il totale sotto i diciotto
#: anche nel caso storto, che e' la condizione perche' l'operatore non venga
#: mai dichiarato in timeout e spento.
BUDGET_PAGINE = 6.0


def _con_offset(payload: dict, offset: int) -> dict:
    """Lo stesso payload spostato di una pagina.

    Copia invece di mutare: le pagine partono insieme e si passano lo stesso
    dizionario, quindi scriverci dentro darebbe a tutte l'ultimo `offset`
    assegnato. Vale anche per chi il payload lo tiene da parte — un log, una
    fixture, un test — che deve vedere la richiesta partita davvero."""
    return {**payload, "criteria": {**payload["criteria"], "offset": offset}}


def _ora_di_partenza(ctx: SearchContext) -> str:
    """L'orario da cui chiedere le soluzioni, nel formato del backend."""
    if ctx.depart_after is None:
        return PRIMA_ORA
    return ctx.depart_after.strftime("%H:%M:%S.000")


def uic_to_location_id(uic: str) -> str | None:
    """Converte l'UIC del dataset (8300219) nell'ID usato da lefrecce (830000219).

    Le due numerazioni condividono le ultime cinque cifre: l'UIC ha il prefisso
    di paese `83`, lefrecce usa `8300` seguito dal codice stazione a 5 cifre.
    """
    digits = "".join(ch for ch in str(uic) if ch.isdigit())
    if len(digits) < 5:
        return None
    return "8300" + digits[-5:]


@register
class Trenitalia(Provider):
    id = "trenitalia"
    name = "Trenitalia"
    mode = Mode.RAIL
    tier = 1
    countries = frozenset({"IT", "CH", "FR", "AT", "SI", "DE"})
    website = "https://www.lefrecce.it/"
    sample_route = ("Torino", "Milano")

    def cache_key(self, origin: Node, destination: Node, ctx: SearchContext) -> str:
        # L'ora minima entra nella richiesta, quindi deve entrare nella chiave:
        # due ricerche sulla stessa tratta e data ricevono risposte diverse, e
        # riusarne una per l'altra rimetterebbe il difetto esattamente dov'era,
        # ma senza traffico di rete che lo renda visibile.
        return f"{super().cache_key(origin, destination, ctx)}:{_ora_di_partenza(ctx)}"

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        origin_id = uic_to_location_id(self.native_id(origin) or "")
        dest_id = uic_to_location_id(self.native_id(destination) or "")
        if not origin_id or not dest_id:
            raise NotServed(f"{self.id}: identificatori mancanti")

        payload = {
            "departureLocationId": int(origin_id),
            "arrivalLocationId": int(dest_id),
            # Il backend restituisce le soluzioni **a partire** dall'orario
            # indicato, dieci per richiesta: `limit` viene ignorato (misurato il
            # 2026-08-17 su Firenze SMN -> Torino Porta Nuova, dove 30 e 100
            # rispondono le stesse dieci soluzioni, 00:40 -> 07:53). Chiedere
            # sempre dalla mezzanotte vuol dire quindi non vedere mai il
            # pomeriggio di una tratta frequentata. Quando chi cerca ha gia'
            # detto da che ora puo' partire, quella e' l'ora giusta da cui
            # guardare: stessa richiesta, stesso costo, e la parte di giornata
            # che gli serve. Senza vincolo si riparte dalla mezzanotte.
            "departureTime": f"{ctx.date.isoformat()}T{_ora_di_partenza(ctx)}",
            "adults": max(1, ctx.pax),
            "children": 0,
            "criteria": {
                "frecceOnly": False,
                "regionalOnly": False,
                "intercityOnly": False,
                "tourismOnly": False,
                "noChanges": False,
                "order": "DEPARTURE_DATE",
                "limit": PAGINA,
                "offset": 0,
            },
            "advancedSearchRequest": {"bestFare": False, "bikeFilter": False},
        }

        # La giornata si prende a pagine, perche' non c'e' altro modo di
        # prenderla. La prima si aspetta da sola e fa da sonda: se torna corta
        # non c'e' altro, e la tratta con pochi treni costa **una** richiesta
        # esatta come prima che la paginazione esistesse.
        prima = await self._una_pagina(_con_offset(payload, 0), ctx)
        soluzioni: list[Any] = list(prima.get("solutions") or [])

        if len(soluzioni) >= PAGINA and ctx.is_endpoint_pair(origin, destination):
            # Le altre partono insieme. `offset` e' assoluto e non dipende da
            # quello che torna prima, quindi metterle in fila indiana era solo
            # attesa: misurate il 2026-08-17, cinque pagine sequenziali sono
            # **11,94 s** contro i 18 s di budget di un provider `tier 1`.
            #
            # E hanno un tempo. Una ricerca tocca decine di coppie di stazioni,
            # tutte verso lo stesso host: se la coda si allunga, aspettarle
            # comunque porterebbe la chiamata oltre il budget, e tre volte di
            # fila il circuito si apre — cioe' i treni spariscono del tutto
            # dalla classifica. Il caso peggiore deve essere «qualche corsa in
            # meno», mai «nessun treno».
            compiti = [
                asyncio.ensure_future(
                    self._una_pagina(_con_offset(payload, p * PAGINA), ctx, retries=0)
                )
                for p in range(1, MAX_PAGINE)
            ]
            arrivati, in_ritardo = await asyncio.wait(compiti, timeout=BUDGET_PAGINE)
            for compito in in_ritardo:
                compito.cancel()
            # Si aspettano anche i cancellati, o restano eccezioni non raccolte
            # che compaiono molto dopo, in un log dove nessuno le collega a qui.
            await asyncio.gather(*in_ritardo, return_exceptions=True)

            for compito in arrivati:
                # Una pagina persa toglie **solo se stessa**: il mattino vale
                # anche senza il pomeriggio, mentre sollevare qui farebbe
                # fallire il provider intero per qualche corsa mancante. Per lo
                # stesso motivo queste non ritentano: tre tentativi su una
                # pagina di scorta costano piu' di quello che salvano.
                if compito.exception() is not None:
                    logger.debug(
                        "%s: una pagina non e' arrivata: %s", self.id, compito.exception()
                    )
                    continue
                soluzioni.extend(compito.result().get("solutions") or [])

        risposta = dict(prima)
        risposta["solutions"] = soluzioni
        return risposta

    async def _una_pagina(
        self, payload: dict, ctx: SearchContext, *, retries: int = 2
    ) -> dict:
        try:
            response = await ctx.http.post(
                SOLUTIONS_URL,
                json=payload,
                headers={"Accept": "application/json"},
                allow_status={400},
                retries=retries,
            )
        except HttpError as exc:
            raise ProviderError(str(exc)) from exc

        if response.status_code == 400:
            return _no_solutions_or_raise(response)

        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"risposta non JSON: {response.text[:200]}") from exc

    # --------------------------------------------------------------- parsing

    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        if not isinstance(raw, dict):
            raise ProviderError("risposta inattesa: non e' un oggetto JSON")

        legs: list[Leg] = []
        for item in raw.get("solutions") or []:
            solution = item.get("solution") if isinstance(item, dict) else None
            if not isinstance(solution, dict):
                continue
            leg = self._to_leg(solution, origin, destination, item)
            if leg is not None:
                legs.append(leg)
        return legs

    def _to_leg(
        self, solution: dict, origin: Node, destination: Node, item: dict | None = None
    ) -> Leg | None:
        depart = _parse_dt(solution.get("departureTime"))
        arrive = _parse_dt(solution.get("arrivalTime"))
        if depart is None or arrive is None or arrive <= depart:
            return None

        trains = [t for t in solution.get("trains") or [] if isinstance(t, dict)]
        # Le tratte urbane sono spezzoni di metropolitana venduti insieme: non
        # contano come cambio percepito e sporcherebbero la descrizione.
        named = [t for t in trains if t.get("trainCategory") not in URBAN_CATEGORIES]
        vehicle = " + ".join(
            f"{t.get('trainCategory') or t.get('acronym') or ''} {t.get('name') or ''}".strip()
            for t in (named or trains)
        ) or None

        status = str(solution.get("status") or "SALEABLE").upper()
        if status == SOLD_OUT:
            # Il prezzo che accompagna una soluzione esaurita **non e' il suo
            # prezzo**: e' quello che resta da vendere. Un Torino-Roma con il
            # Frecciarossa 9583 pieno viene proposto a 1,50 euro, cioe' il solo
            # regionale finale, e con quella cifra vince qualunque classifica.
            # Verificato il 27 luglio 2026: stessa tratta, stessa struttura, le
            # soluzioni acquistabili costavano fra 105 e 141 euro.
            # Un viaggio che non si puo' comprare non e' un'opzione, quindi la
            # soluzione esce del tutto: e' la stessa regola per cui non si
            # propone un treno FAL nei giorni in cui non circola.
            return None

        price = solution.get("price") or {}
        amount = price.get("amount")
        fare: Fare | None = None
        notes: list[str] = []
        if status != "SALEABLE":
            # Stato inatteso: l'orario resta valido, il prezzo no. Meglio farlo
            # stimare da `routing/cost.py`, che lo dichiara, che riportarne uno
            # di cui non conosciamo il significato.
            notes.append(f"non acquistabile online ({status.lower()}): prezzo da verificare")
        elif isinstance(amount, (int, float)) and not price.get("hideAmount"):
            indicativo = bool(price.get("indicative"))
            fare = Fare(
                amount=float(amount),
                currency="EUR",
                fare_class=None,
                # Trenitalia non e' rimborsabile sulle tariffe scontate; senza
                # dettaglio dell'offerta non possiamo affermarlo, quindi None.
                refundable=None,
                included_cabin_bags=1,
                included_checked_bags=1,
                # Non basta la nota: quella la legge chi apre il dettaglio,
                # mentre il totale in grande resta uguale a quello di una cifra
                # certa. Questo lo legge il calcolo del costo, che marca la voce.
                indicative=indicativo,
            )
            if indicativo:
                notes.append("prezzo indicativo: 'a partire da'")
        else:
            notes.append("prezzo non esposto dal motore di ricerca")

        reduced: dict[str, float] = {}
        if fare is not None and item is not None:
            ridotta = _conditional_total(item, float(amount))
            if ridotta is not None:
                totale, condizioni = ridotta
                notes.append(
                    f"con {' e '.join(sorted(condizioni))}: {totale:.2f} EUR invece di "
                    f"{amount:.2f} — prezzo dell'operatore, se ne hai diritto"
                )
                # La nota la legge chi guarda; questo lo legge il calcolo del
                # costo. Chi ha dichiarato la tessera giusta paga questa cifra
                # in classifica invece di una percentuale stimata, e il totale
                # smette di invecchiare insieme al listino.
                reduced = {nome: totale for nome in condizioni}

        segments = [
            f"{node.get('origin')} {_hhmm(node.get('departureTime'))}"
            f" -> {node.get('destination')} {_hhmm(node.get('arrivalTime'))}"
            for node in solution.get("nodes") or []
            if isinstance(node, dict)
        ]

        distance_km = _straight_km(origin, destination)
        return Leg(
            provider=self.id,
            mode=Mode.RAIL,
            origin=origin,
            destination=destination,
            depart=depart,
            arrive=arrive,
            operator=self.name,
            vehicle=vehicle,
            fare=fare,
            booking_url=BOOKING_URL,
            co2_kg=round(distance_km * CO2_KG_PER_KM, 1) if distance_km else None,
            internal_changes=max(0, len(named or trains) - 1),
            segments=segments,
            notes=notes,
            reduced_fares=reduced,
        )


def _price(value: Any) -> float | None:
    if isinstance(value, dict) and isinstance(value.get("amount"), (int, float)):
        return float(value["amount"])
    return None


def _conditional_total(item: dict, esposto: float) -> tuple[float, set[str]] | None:
    """Il totale piu' basso che si pagherebbe avendo la tessera giusta.

    Le tariffe ridotte di Trenitalia **arrivano gia'** nella risposta: stanno in
    `grids[].services[].offers[]` con il loro nome (FrecciaYOUNG, FrecciaSENIOR,
    YOUNG, SENIOR) e il loro prezzo. Non compaiono nel prezzo esposto perche'
    `minPrice` le esclude di proposito: richiedono una tessera e spesso un'eta',
    e il motore non sa se chi cerca ne ha diritto. L'array `discounts` resta
    vuoto per lo stesso motivo — gli sconti non sono modellati li'.

    Qui si calcola solo *quanto* costerebbero, e lo si dice. Non si applica
    niente: presumere una tessera che l'utente non ha significherebbe mostrare
    un prezzo che alla cassa non esiste, e in questo progetto vale la regola
    opposta — nel dubbio si applica meno, perche' uno sconto mancato si scopre
    con piacere e uno inventato fa perdere il viaggio."""
    grids = item.get("grids") or []
    if not grids:
        return None

    totale = 0.0
    condizioni: set[str] = set()
    for grid in grids:
        migliore_libera: float | None = None
        migliore_assoluta: float | None = None
        nome_assoluta: str | None = None

        for service in grid.get("services") or []:
            minimo = _price(service.get("minPrice"))
            if minimo is not None:
                migliore_libera = minimo if migliore_libera is None else min(migliore_libera, minimo)
            for offer in service.get("offers") or []:
                if not isinstance(offer, dict) or offer.get("status") != "SALEABLE":
                    continue
                prezzo = _price(offer.get("price"))
                if prezzo is None:
                    continue
                if migliore_assoluta is None or prezzo < migliore_assoluta:
                    migliore_assoluta, nome_assoluta = prezzo, str(offer.get("name") or "")

        if migliore_assoluta is None:
            return None
        totale += migliore_assoluta
        if (
            migliore_libera is not None
            and migliore_assoluta < migliore_libera - 0.01
            and nome_assoluta
        ):
            condizioni.add(nome_assoluta)

    if not condizioni or totale >= esposto - 0.01:
        return None
    return round(totale, 2), condizioni


def _no_solutions_or_raise(response: Any) -> dict:
    """Trenitalia risponde 400 anche quando semplicemente non ci sono treni.

    "Non abbiamo trovato nessuna soluzione per i criteri selezionati" e' un
    esito legittimo di una ricerca, non un guasto: trattarlo come errore fa
    scattare il circuit breaker e spegne l'adapter per tutte le altre tratte
    della stessa ricerca. Un 400 che non e' quello resta un errore vero."""
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"HTTP 400 non interpretabile: {response.text[:200]}") from exc

    if isinstance(payload, dict) and payload.get("type") == "ERROR":
        logger.debug("nessuna soluzione: %s", payload.get("message"))
        return {"solutions": [], "message": payload.get("message")}

    raise ProviderError(f"HTTP 400: {response.text[:200]}")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _hhmm(value: Any) -> str:
    parsed = _parse_dt(value)
    return parsed.strftime("%H:%M") if parsed else "?"


def _straight_km(origin: Node, destination: Node) -> float:
    from app.models import node_distance_km

    # Il percorso ferroviario e' piu' lungo della linea d'aria; il fattore 1.2
    # e' la correzione usuale per la rete europea.
    return node_distance_km(origin, destination) * 1.2
