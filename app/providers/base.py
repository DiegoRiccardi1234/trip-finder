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
    http: HttpClient = field(default_factory=get_http_client)

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
        key = self.cache_key(origin, destination, ctx)
        raw = await cache.get(key)
        if raw is None:
            raw = await self.fetch(origin, destination, ctx)
            await cache.set(key, raw, kind=self.cache_kind)
        return self.parse(raw, origin, destination, ctx)

    def __repr__(self) -> str:  # pragma: no cover - diagnostica
        return f"<{type(self).__name__} id={self.id} mode={self.mode.value} tier={self.tier}>"
