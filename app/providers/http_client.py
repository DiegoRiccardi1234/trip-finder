"""Client HTTP per gli adapter, costruito su curl_cffi.

Perche' non `httpx`/`requests`: i siti degli operatori dietro Cloudflare, Akamai
o Imperva non guardano gli header, guardano il **fingerprint TLS**. Una libreria
Python normale ha un ClientHello riconoscibile e prende 403 anche con gli header
perfetti. `curl_cffi` replica il ClientHello di Chrome, quindi la maggior parte
dei target si apre senza dover avviare un browser vero (che costa 10-20 secondi
per ricerca invece di 1-2).

Il client aggiunge tre cose sopra curl_cffi:
  - una sessione persistente per host, cosi' i cookie si scaldano e restano;
  - un rate limit per host, perche' il modo piu' rapido per farsi bloccare e'
    sparare dieci richieste in parallelo allo stesso dominio;
  - il riconoscimento esplicito del blocco anti-bot, che va distinto da un
    errore di rete: nel primo caso ha senso passare al browser, nel secondo no.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession, Response

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class HttpError(Exception):
    """Errore di rete o risposta inattesa: ritentabile."""


class Blocked(HttpError):
    """Il target ha risposto con un muro anti-bot. Va escalato al browser."""


#: Marcatori tipici delle pagine di sfida. Cercati solo su risposte HTML corte.
BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "cf-challenge",
    "cf_chl_opt",
    "checking your browser",
    "access denied",
    "px-captcha",
    "incapsula",
    "distil_r_captcha",
    "are you a robot",
    "unusual traffic",
)
BLOCK_STATUSES = frozenset({401, 403, 429, 503})

DEFAULT_HEADERS = {
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


#: Richieste contemporanee verso lo stesso host. Serve a sovrapporre le latenze
#: senza trasformare la ricerca in una raffica.
MAX_CONCURRENT_PER_HOST = 4

#: Host che sopportano un ritmo piu' alto del limite generale, in richieste al
#: secondo.
#:
#: Il limite normale e' pensato per i motori di prenotazione, che una raffica
#: la notano e la bloccano. `services-api.ryanair.com` non e' uno di quelli: e'
#: l'endpoint pubblico delle tariffe, risponde JSON minuscoli e non chiede
#: nulla. La distinzione serve da quando gli aeroporti considerati non hanno
#: piu' un tetto: da Torino sono quattro, e ogni coppia scalo-scalo e' una
#: richiesta a questo host. A una al secondo le ultime aspettavano piu' del
#: loro timeout (18 s) e morivano tutte insieme, aprendo il circuito
#: dell'unico adapter aereo che abbiamo — cioe' facendo sparire i voli dalla
#: classifica senza che nessuno se ne accorgesse.
HOST_RATE_OVERRIDES: dict[str, float] = {
    "services-api.ryanair.com": 6.0,
    "www.ryanair.com": 6.0,
}


class _HostGate:
    """Distanzia nel tempo le richieste verso lo stesso host.

    Il ritmo si applica agli **inizi** delle richieste, non alla loro durata:
    tenere il lock fino alla risposta ridurrebbe la concorrenza per dominio a
    uno, e con un secondo di latenza a richiesta cinquanta interrogazioni
    diventerebbero un minuto pieno."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._pace = asyncio.Lock()
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_PER_HOST)
        self._next_allowed = 0.0

    async def __aenter__(self) -> None:
        await self._slots.acquire()
        async with self._pace:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed = max(now, self._next_allowed) + self._min_interval

    async def __aexit__(self, *_: object) -> None:
        self._slots.release()


class HttpClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._sessions: dict[str, AsyncSession] = {}
        self._gates: dict[str, _HostGate] = {}
        self._session_lock = asyncio.Lock()
        self._global = asyncio.Semaphore(self._settings.search_max_concurrency)

    # ---------------------------------------------------------------- interno

    async def _session_for(self, host: str) -> AsyncSession:
        session = self._sessions.get(host)
        if session is not None:
            return session
        async with self._session_lock:
            if host not in self._sessions:
                self._sessions[host] = AsyncSession(
                    impersonate=self._settings.impersonate_profile,
                    timeout=self._settings.http_timeout,
                    headers=dict(DEFAULT_HEADERS),
                )
        return self._sessions[host]

    def _gate_for(self, host: str) -> _HostGate:
        gate = self._gates.get(host)
        if gate is None:
            rate = HOST_RATE_OVERRIDES.get(host, self._settings.domain_rate_limit)
            gate = _HostGate(1.0 / max(0.05, rate))
            self._gates[host] = gate
        return gate

    @staticmethod
    def _looks_blocked(response: Response) -> bool:
        if response.status_code in BLOCK_STATUSES:
            return True
        content_type = (response.headers.get("content-type") or "").lower()
        if "html" not in content_type:
            return False
        # Le pagine di sfida sono piccole; ispezionare una pagina vera da 1 MB
        # sarebbe sprecato e darebbe falsi positivi sui testi legali.
        if len(response.content) > 60_000:
            return False
        body = response.text[:6000].lower()
        return any(marker in body for marker in BLOCK_MARKERS)

    # ---------------------------------------------------------------- pubblico

    async def request(
        self,
        method: str,
        url: str,
        *,
        retries: int = 2,
        backoff: float = 1.5,
        allow_status: frozenset[int] | set[int] = frozenset(),
        **kwargs: Any,
    ) -> Response:
        host = host_of(url)
        session = await self._session_for(host)
        gate = self._gate_for(host)
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                async with self._global:
                    async with gate:
                        response = await session.request(method, url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - curl_cffi alza tipi propri
                last_error = HttpError(f"{type(exc).__name__}: {exc}")
                logger.debug("%s %s tentativo %d fallito: %s", method, url, attempt + 1, exc)
            else:
                if response.status_code in allow_status:
                    return response
                if self._looks_blocked(response):
                    # Un 429 e' throttling e a volte passa aspettando; un 403 no.
                    if response.status_code == 429 and attempt < retries:
                        last_error = Blocked(f"HTTP 429 da {host}")
                    else:
                        raise Blocked(f"HTTP {response.status_code} da {host}")
                elif response.status_code >= 500:
                    last_error = HttpError(f"HTTP {response.status_code} da {host}")
                elif response.status_code >= 400:
                    raise HttpError(
                        f"HTTP {response.status_code} da {host}: {response.text[:200]}"
                    )
                else:
                    return response

            if attempt < retries:
                await asyncio.sleep(backoff * (2**attempt))

        raise last_error or HttpError(f"{method} {url} fallita")

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Response:
        return await self.request("POST", url, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.get(url, **kwargs)
        return self._parse_json(response, url)

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.post(url, **kwargs)
        return self._parse_json(response, url)

    @staticmethod
    def _parse_json(response: Response, url: str) -> Any:
        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            snippet = response.text[:200].replace("\n", " ")
            raise HttpError(f"risposta non JSON da {url}: {snippet}") from exc

    async def warm_up(self, url: str) -> None:
        """Visita una pagina per raccogliere i cookie prima delle chiamate API.

        Molti endpoint interni rifiutano la prima richiesta se arriva senza i
        cookie che il sito imposta alla home."""
        try:
            await self.get(url, retries=0)
        except HttpError as exc:
            logger.debug("warm-up di %s non riuscito: %s", url, exc)

    async def close(self) -> None:
        for session in self._sessions.values():
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()


_client: HttpClient | None = None


def get_http_client() -> HttpClient:
    global _client
    if _client is None:
        _client = HttpClient()
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
