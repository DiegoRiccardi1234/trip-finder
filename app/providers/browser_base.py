"""Classi base per gli adapter che richiedono un browser.

Una parte consistente degli operatori europei rifiuta le richieste dirette:
Deutsche Bahn risponde `403 OPS_BLOCKED`, Trainline serve un captcha, Italo
vuole un token che esiste solo dentro la sessione web. Con un browser vero
passano quasi tutti, al prezzo di dieci-venti secondi per ricerca invece di uno.

Due forme, che coprono i casi reali:

  - `BrowserJsonProvider` carica una pagina del sito e poi chiama l'API **da
    dentro quella pagina**. La richiesta eredita cookie, token e origine, e per
    il server e' indistinguibile dal sito che chiama se stesso. E' la forma
    preferibile: la risposta e' JSON strutturato, quindi il parser regge nel
    tempo. Caso tipico: Italo.
  - `BrowserFormProvider` apre l'URL di ricerca, aspetta che compaiano i
    risultati e restituisce l'HTML. Piu' fragile, perche' dipende dalla forma
    della pagina, ma e' l'unica via quando l'API non e' raggiungibile nemmeno
    dall'interno. Caso tipico: Deutsche Bahn.

In entrambe `fetch` fa la rete e `parse` no, come per gli adapter HTTP: cosi' i
test del parser girano sulle fixture salvate, senza browser e senza rete.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Any

from app.models import Node
from app.providers.base import NotServed, Provider, ProviderError, SearchContext
from app.providers.browser_pool import BrowserUnavailable, get_browser_pool

logger = logging.getLogger(__name__)

#: Quanto aspettare che i risultati compaiano su una pagina di ricerca.
RESULTS_TIMEOUT_MS = 35_000


class BrowserProvider(Provider):
    """Parte comune: un solo posto dove gestire il browser che non parte."""

    tier = 2
    #: Pagina da caricare per prima, quella che porta i cookie di sessione.
    site_url: str = ""

    @property
    def pool(self):
        return get_browser_pool()

    def _guard(self) -> None:
        """Un browser assente non e' un guasto dell'adapter.

        Se Chromium non e' installato tutti gli adapter tier 2 fallirebbero uno
        per uno, riempiendo la diagnostica di errori identici e aprendo circuiti
        che non c'entrano nulla. Qui diventa `NotServed`, che la ricerca segna
        come 'saltato' e non conta come guasto."""
        if not self.pool.available:
            raise NotServed("browser non disponibile: adapter saltato")


class BrowserJsonProvider(BrowserProvider):
    """Chiama un'API JSON dal contesto di una pagina del sito."""

    api_url: str = ""
    api_method: str = "POST"
    #: Millisecondi di attesa dopo il caricamento, per lasciar scrivere i cookie.
    settle_ms: int = 1500

    @abstractmethod
    def build_body(
        self, origin: Node, destination: Node, ctx: SearchContext
    ) -> Any | None:
        """Corpo della richiesta. `None` per una GET senza corpo."""

    def build_headers(
        self, origin: Node, destination: Node, ctx: SearchContext
    ) -> dict[str, str]:
        return {}

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        self._guard()
        try:
            result = await self.pool.fetch_json(
                self.site_url,
                self.api_url,
                method=self.api_method,
                body=self.build_body(origin, destination, ctx),
                headers=self.build_headers(origin, destination, ctx),
                wait_ms=self.settle_ms,
            )
        except BrowserUnavailable as exc:
            raise NotServed(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc

        if not isinstance(result, dict):
            raise ProviderError("risposta inattesa dal contesto di pagina")
        if not result.get("ok"):
            detail = result.get("text") or result.get("data") or ""
            raise ProviderError(
                f"HTTP {result.get('status')} dall'API interna: {str(detail)[:200]}"
            )
        return result.get("data")


class BrowserFormProvider(BrowserProvider):
    """Apre la pagina di ricerca dell'operatore e ne restituisce l'HTML."""

    #: Selettore che compare quando i risultati sono pronti. Senza, si aspetta
    #: solo il caricamento, che spesso arriva prima dei risultati.
    results_selector: str = ""
    #: Selettore che compare quando la ricerca non ha prodotto nulla: distingue
    #: "nessuna corsa quel giorno" da "la pagina non ha mai caricato".
    empty_selector: str = ""
    settle_ms: int = 1200

    @abstractmethod
    def search_url(self, origin: Node, destination: Node, ctx: SearchContext) -> str:
        """URL profondo che apre direttamente i risultati per questa tratta."""

    async def prepare(self, page, origin: Node, destination: Node, ctx: SearchContext) -> None:
        """Passi extra dopo il caricamento: accettare i cookie, premere Cerca."""

    async def fetch(self, origin: Node, destination: Node, ctx: SearchContext) -> Any:
        self._guard()
        url = self.search_url(origin, destination, ctx)
        try:
            async with self.pool.page() as page:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(self.settle_ms)
                await self.prepare(page, origin, destination, ctx)

                if self.results_selector:
                    selector = self.results_selector
                    if self.empty_selector:
                        selector = f"{self.results_selector}, {self.empty_selector}"
                    try:
                        await page.wait_for_selector(selector, timeout=RESULTS_TIMEOUT_MS)
                    except Exception as exc:  # noqa: BLE001
                        raise ProviderError(
                            "i risultati non sono comparsi entro il tempo previsto"
                        ) from exc
                return await page.content()
        except BrowserUnavailable as exc:
            raise NotServed(str(exc)) from exc
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
