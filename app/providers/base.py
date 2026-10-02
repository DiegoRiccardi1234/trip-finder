"""Interfaccia comune degli adapter.

Ogni operatore e' un plugin isolato: se il suo parser si rompe, gli altri
continuano a funzionare e la ricerca degrada invece di fallire. L'unico
contratto e' `search()` che restituisce gambe normalizzate.

Regole per chi scrive un adapter:
  - gli orari restituiti devono essere timezone-aware (usare `local_dt`);
  - se il provider non copre la tratta si alza `NotServed`, non si torna [];
  - una lista vuota significa "ho cercato e non c'e' nulla quel giorno", ed e'
    un'informazione diversa da "non so rispondere";
  - i prezzi vanno riportati come pubblicati, senza aggiungere supplementi:
    bagagli e trasferimenti li calcola `routing/cost.py` in modo uniforme.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.models import Leg, Mode, Node
from app.orchestrator import cache
from app.providers.http_client import HttpClient, get_http_client

logger = logging.getLogger(__name__)

DEFAULT_TZ = ZoneInfo("Europe/Rome")


def city_key(node: Node) -> str:
    """Nome della citta' di un nodo, per gli operatori che ragionano per citta'.

    Molte fermate non portano il campo `city`; il nome della stazione inizia
    quasi sempre con la citta' ("Torino Porta Nuova", "Bari Centrale"), quindi
    si usa quello come ripiego."""
    if node.city:
        return node.city.strip().lower()
    return node.name.split(",")[0].split("(")[0].strip().lower()


#: Quanto puo' distare, al massimo, la fermata dichiarata da una gamba da
#: quella che era stata chiesta. Misurato il 2026-08-22 su tutte le 150 gambe
#: delle fixture salvate: lo scarto e' **zero** per ogni adapter sano, perche'
#: tutti costruiscono la gamba sui nodi richiesti. Cento chilometri sono quindi
#: larghi di proposito: qui non si tara una tolleranza fine, si mette una rete
#: sotto un errore di categoria — una gamba di un'altra regione.
SCARTO_MASSIMO_KM = 100.0

#: Quanto puo' essere lenta una gamba vera, e l'attesa che le si concede
#: comunque. Sono le due meta' della stessa domanda: questa durata sta in piedi
#: per questa distanza?
#:
#: La soglia e' **la velocita' di una persona che cammina**, e non e' una scelta
#: arbitraria: e' l'unica riga che si puo' difendere davanti a un caso vero.
#: Sopra ci sta qualunque viaggio, per quanto storto; sotto non ci sta niente
#: che abbia senso chiamare collegamento.
#:
#: Era 8 km/h, tarata sulle fixture, e il 2026-08-22 ha scartato una gamba
#: **vera**: Trenitalia vende Bari Centrale -> Matera come Frecciarossa 8302 +
#: 9583 + FrecciaLink, 07:40 -> 19:20, cioe' 55 km in linea d'aria e settecento
#: minuti — un biglietto assurdo ma acquistabile, che sale a nord e ritorna.
#: E' il promemoria che una soglia tarata solo sui dati che si hanno in mano
#: mangia i casi che non si sono guardati. Con 5 km/h e due ore di attesa quel
#: viaggio passa con un margine del dieci per cento, mentre la gamba sbagliata
#: che ha fatto nascere il controllo — Asti-Canelli servita con una corsa
#: Toscana-Calabria, 21 km in 1091 minuti — sfora ancora di tre volte.
#:
#: L'attesa serve alle gambe corte, dove la velocita' media non dice niente:
#: due chilometri in venti minuti sono un autobus urbano normale, non un
#: errore. Senza questo termine il controllo scarterebbe proprio il trasporto
#: locale, che e' la copertura che manca di piu'.
VELOCITA_MINIMA_KMH = 5.0
ATTESA_CONCESSA_MIN = 120.0


def perche_non_risponde(leg: Leg, origin: Node, destination: Node) -> str:
    """Perche' questa gamba non risponde alla domanda fatta, o stringa vuota.

    Nasce da un difetto vero, trovato il 2026-08-22: una ricerca Asti->Canelli
    ha ricevuto da FlixBus una corsa **Castiglione della Pescaia -> Lamezia
    Terme**, che e' entrata in classifica come se fosse la risposta, con 18 ore
    di viaggio per ventun chilometri.

    Nessun controllo a valle se ne era accorto, e il motivo merita di restare
    scritto: guardavano tutti le **coordinate**, e quella gamba portava le
    coordinate della tratta chiesta con i nomi di un'altra. Le prende in
    prestito `flixbus._station_node` quando la fermata non e' nel dataset, che
    misurato e' il caso normale e non l'eccezione. Da qui i due controlli: uno
    geografico, che serve agli adapter che dicono la verita' sulle coordinate,
    e uno sulla **durata**, che e' l'unico segnale che una coordinata presa in
    prestito non puo' falsificare.

    Uno scarto qui non e' un guasto dell'operatore: e' una gamba sola che non
    risponde alla domanda. Chi ne sbaglia una su venti resta utile per le altre
    diciannove.
    """
    from app.models import node_distance_km

    da_origine = node_distance_km(origin, leg.origin)
    da_arrivo = node_distance_km(destination, leg.destination)
    if da_origine > SCARTO_MASSIMO_KM or da_arrivo > SCARTO_MASSIMO_KM:
        return (
            f"parte a {da_origine:.0f} km da {origin.name} "
            f"e arriva a {da_arrivo:.0f} km da {destination.name}"
        )

    km = node_distance_km(leg.origin, leg.destination)
    plausibile = ATTESA_CONCESSA_MIN + km / VELOCITA_MINIMA_KMH * 60.0
    if leg.duration_min > plausibile:
        return (
            f"{km:.0f} km in {leg.duration_min} minuti, "
            f"oltre il massimo plausibile di {plausibile:.0f}"
        )
    return ""


class ProviderError(Exception):
    """Errore recuperabile dell'adapter: la ricerca prosegue senza di lui."""


class NotServed(ProviderError):
    """Il provider non copre questa tratta. Non conta come guasto."""


@dataclass
class SearchContext:
    """Tutto cio' di cui un adapter ha bisogno oltre a origine e destinazione."""

    date: date
    pax: int = 1
    with_checked_bag: bool = False
    currency: str = "EUR"
    #: L'ora minima di partenza dichiarata da chi cerca, se c'e'. Non e' un
    #: filtro — quello lo applica il ranker — ma un'informazione che permette a
    #: un operatore di rispondere sulla parte di giornata che interessa davvero.
    #: Serve dove il backend pagina: Trenitalia restituisce dieci soluzioni per
    #: richiesta a partire dall'orario indicato, quindi chiedere sempre dalla
    #: mezzanotte significa non vedere mai il pomeriggio. Chi la usa deve
    #: metterla anche nella propria `cache_key`, o si riprende la risposta
    #: dell'altra ora.
    depart_after: time | None = None
    #: Le fermate della partenza e dell'arrivo **chiesti dall'utente**. Le
    #: coppie intermedie di un percorso con cambio non ci stanno dentro.
    #: Serve a chi puo' spendere di piu' su una tratta sola: vedi
    #: `is_endpoint_pair`.
    endpoints: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
    http: HttpClient = field(default_factory=get_http_client)

    @property
    def endpoints_noti(self) -> bool:
        """Se qualcuno ha dichiarato quali sono gli estremi della ricerca.

        Serve a distinguere due `False` diversi di `is_endpoint_pair`: «questa
        non e' la tratta cercata» e «non so quale sia la tratta cercata». Il
        secondo caso e' quello degli script di diagnostica, che costruiscono un
        contesto nudo: senza questa distinzione un adapter prudente li vedrebbe
        come coincidenze intermedie, tacerebbe sempre, e `check_providers.py`
        lo dichiarerebbe rotto quando invece sta obbedendo."""
        partenze, arrivi = self.endpoints
        return bool(partenze or arrivi)

    def is_endpoint_pair(self, origin: Node, destination: Node) -> bool:
        """Vero se questa e' la tratta che l'utente ha chiesto, non un pezzo.

        Un operatore che pagina non puo' permettersi la giornata intera su ogni
        coppia di stazioni: una ricerca ne tocca una ventina, e moltiplicare le
        richieste allunga la coda verso quell'host finche' le ultime sforano il
        proprio budget e l'operatore viene dichiarato guasto. Misurato il
        2026-08-17 su Prato -> Torino: venti coppie, cinque pagine a testa, cento
        richieste, ventinove secondi di coda contro diciotto di budget.

        Qui si dice dove vale la pena spendere: la tratta che si sta cercando.
        Sulle coincidenze intermedie la prima pagina e' quello che c'e'."""
        partenze, arrivi = self.endpoints
        return origin.id in partenze and destination.id in arrivi

    def local_dt(self, node: Node, day: date, clock: time) -> datetime:
        """Compone un datetime aware nel fuso della fermata."""
        tz = ZoneInfo(node.timezone) if node.timezone else DEFAULT_TZ
        return datetime.combine(day, clock, tzinfo=tz)


class Provider(ABC):
    #: Identificatore stabile, usato nelle chiavi di cache e nei provider_ids.
    id: str
    #: Nome mostrato all'utente.
    name: str
    mode: Mode
    #: 1 = solo HTTP (veloce), 2 = richiede un browser (lento, ultima risorsa).
    tier: int = 1
    #: Paesi serviti in ISO-2. `None` significa nessun vincolo.
    countries: frozenset[str] | None = None
    #: Vero se l'adapter restituisce prezzi, falso se solo orari.
    has_prices: bool = True
    #: Vero se le tariffe sono stimate o statiche, non lette live.
    is_static: bool = False
    #: "node"  -> l'operatore ragiona per fermata (treni, voli, traghetti);
    #: "city"  -> l'operatore ragiona per citta' (FlixBus, aggregatori).
    #: Serve a non interrogare dieci volte lo stesso operatore per dieci fermate
    #: della stessa citta', che darebbero le stesse identiche corse.
    granularity: str = "node"
    #: Tratta che questo operatore serve di sicuro, usata da
    #: `scripts/check_providers.py` per accorgersi che il parser si e' rotto.
    #: Va scelta fra le rotte di punta: se non torna nulla li', e' un guasto,
    #: non una giornata senza corse.
    sample_route: tuple[str, str] | None = None
    #: Giorno in cui quella tratta e' stata vista funzionare, in ISO.
    #: Serve per gli operatori stagionali o a corsa unica: la loro linea puo'
    #: non circolare in una data qualunque, e senza questo riferimento il
    #: controllo di salute li segnerebbe rotti quando semplicemente non e'
    #: giorno di servizio. Quando la data e' passata, il controllo lo dice e
    #: usa un giorno futuro: e' il segnale che va rinnovata.
    sample_date: str | None = None
    #: Pagina da aprire quando non possiamo interrogare l'operatore ma vogliamo
    #: comunque mandarci l'utente.
    website: str = ""

    def native_id(self, node: Node) -> str | None:
        return node.provider_ids.get(self.id)

    def supports_node(self, node: Node) -> bool:
        """Se l'adapter sa identificare questa fermata.

        Il caso normale e' avere il proprio identificatore nel dataset. Chi usa
        una chiave universale deve ridefinire questo metodo: le compagnie aeree
        parlano di codici IATA e non compaiono nei `provider_ids`, e senza
        questa distinzione i loro nodi verrebbero scartati prima di provarci."""
        return self.native_id(node) is not None

    def can_serve(self, origin: Node, destination: Node) -> bool:
        """Filtro rapido prima di spendere una richiesta di rete."""
        if origin.id == destination.id:
            return False
        if self.countries is not None:
            countries = {origin.country, destination.country}
            if not countries <= set(self.countries) | {None}:
                return False
        return self.supports_node(origin) and self.supports_node(destination)

    async def sample_nodes(self, ctx: SearchContext) -> tuple[Node, Node] | None:
        """Fermate da usare per la prova di salute, se l'adapter sa fornirle.

        Il controllo di salute deve verificare **l'adapter**, non il gazetteer
        del progetto. Molti operatori servono paesi piccoli che il nostro indice
        geografico non conosce: passare dal resolver trasformerebbe un adapter
        sano in un errore che non c'entra nulla. Chi ha un proprio catalogo di
        fermate lo usa qui; gli altri tornano `None` e si passa dal resolver."""
        return None

    def route_key(self, origin: Node, destination: Node) -> tuple[str, str, str]:
        """Chiave di deduplica delle richieste all'interno di una ricerca."""
        if self.granularity == "city":
            return (self.id, city_key(origin), city_key(destination))
        return (self.id, origin.id, destination.id)

    def scope(self, origin: Node, destination: Node) -> str:
        """Chiave del circuit breaker: separa i guasti nazionali dagli esteri."""
        if origin.country and destination.country and origin.country != destination.country:
            return "international"
        return "domestic"

    @abstractmethod
    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        """Scarica la risposta grezza dell'operatore. Nessuna interpretazione qui."""

    @abstractmethod
    def parse(
        self, raw: Any, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        """Trasforma la risposta grezza in gambe normalizzate.

        Volutamente sincrona e senza rete: e' la parte che si rompe quando il
        sito cambia, e deve poter girare su una fixture salvata."""

    async def prepare_parse(self, ctx: SearchContext) -> None:
        """Prepara le anagrafiche richieste dal parser, anche fuori da search()."""

    #: Per quanto tenere valida una risposta. I prezzi si muovono, ma non fra
    #: un percorso candidato e l'altro della stessa ricerca: senza cache la
    #: stessa tratta verrebbe chiesta piu' volte nello stesso minuto.
    cache_kind: str = "fare"

    def cache_key(self, origin: Node, destination: Node, ctx: SearchContext) -> str:
        return cache.make_key(
            f"raw:{self.id}",
            self.native_id(origin) or origin.id,
            self.native_id(destination) or destination.id,
            ctx.date.isoformat(),
            ctx.pax,
        )

    async def search(
        self, origin: Node, destination: Node, ctx: SearchContext
    ) -> list[Leg]:
        """Cerca le corse dirette fra due fermate in una data."""
        await self.prepare_parse(ctx)
        key = self.cache_key(origin, destination, ctx)
        raw = await cache.get(key)
        if raw is None:
            raw = await self.fetch(origin, destination, ctx)
            await cache.set(key, raw, kind=self.cache_kind)
        return self._solo_coerenti(
            self.parse(raw, origin, destination, ctx), origin, destination
        )

    def _solo_coerenti(
        self, legs: list[Leg], origin: Node, destination: Node
    ) -> list[Leg]:
        """Le gambe che rispondono davvero alla tratta chiesta.

        Sta in `search` e non nei singoli adapter perche' e' l'unico punto da
        cui passano tutti: l'orchestratore chiama solo questo. Un adapter
        scritto domani eredita il controllo senza doverlo sapere, che e'
        esattamente il motivo per cui il difetto di FlixBus era invisibile —
        c'era un posto dove metterlo e non lo usava nessuno.

        Uno scarto non e' un guasto: chi sbaglia una gamba su dieci resta utile
        per le altre nove. Va pero' **detto**, perche' un adapter che comincia
        a produrre gambe incoerenti e' rotto e il log e' l'unico posto in cui
        si vede prima che lo veda l'utente."""
        tenute: list[Leg] = []
        for leg in legs:
            motivo = perche_non_risponde(leg, origin, destination)
            if motivo:
                logger.warning(
                    "%s: scarto una gamba che non risponde alla tratta chiesta "
                    "(%s -> %s): %s",
                    self.id,
                    leg.origin.name,
                    leg.destination.name,
                    motivo,
                )
                continue
            tenute.append(leg)
        return tenute

    def __repr__(self) -> str:  # pragma: no cover - diagnostica
        return f"<{type(self).__name__} id={self.id} mode={self.mode.value} tier={self.tier}>"
