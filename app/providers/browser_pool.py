"""Pool di pagine browser per gli adapter tier 2.

Ultima risorsa: aprire un browser costa secondi e memoria, quindi si usa solo
per i target che non cedono a curl_cffi. Il contesto e' **persistente** su disco
(non incognito) perche' un profilo con cronologia e cookie e' molto meno
sospetto di uno vergine, e perche' evita di rifare ogni volta le sfide.

Si usa `patchright`, fork di Playwright che rimuove le tracce piu' note
dell'automazione. Serve installare i browser una volta:

    .venv\\Scripts\\patchright install chromium
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.config import ROOT, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from patchright.async_api import Page

logger = logging.getLogger(__name__)

PROFILE_DIR = ROOT / ".browser-profiles" / "default"
#: Pagine aperte insieme. Ognuna costa memoria e CPU vere: oltre questa soglia
#: le sessioni si rallentano a vicenda e il tempo totale peggiora invece di
#: migliorare.
MAX_CONCURRENT_PAGES = 4


class BrowserUnavailable(RuntimeError):
    """Il browser non e' installato o non parte: gli adapter tier 2 sono esclusi."""


class BrowserPool:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._playwright = None
        self._context = None
        self._lock = asyncio.Lock()
        self._pages = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
        self._failed = False

    async def _ensure(self) -> None:
        if self._context is not None:
            return
        if self._failed:
            raise BrowserUnavailable("avvio del browser gia' fallito in questa sessione")

        async with self._lock:
            if self._context is not None:
                return
            try:
                from patchright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover
                self._failed = True
                raise BrowserUnavailable("patchright non installato") from exc

            profile: Path = PROFILE_DIR
            profile.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()

            # Chrome vero e' meno rilevabile di Chromium; se manca si ripiega.
            last_error: Exception | None = None
            for channel in ("chrome", None):
                try:
                    self._context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile),
                        channel=channel,
                        headless=self._settings.browser_headless,
                        no_viewport=True,
                        locale="it-IT",
                        timezone_id="Europe/Rome",
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    logger.debug("browser channel=%s non disponibile: %s", channel, exc)
                    continue
                logger.info("browser avviato (channel=%s)", channel or "chromium")
                return

            self._failed = True
            await self._shutdown_playwright()
            raise BrowserUnavailable(f"impossibile avviare il browser: {last_error}")

    @asynccontextmanager
    async def page(self) -> AsyncIterator["Page"]:
        await self._ensure()
        async with self._pages:
            page = await self._context.new_page()  # type: ignore[union-attr]
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    async def fetch_json(
        self,
        site_url: str,
        api_url: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        wait_ms: int = 1200,
    ) -> Any:
        """Chiama un'API JSON **da dentro** una pagina del sito.

        E' il grimaldello per gli operatori che rifiutano le richieste dirette.
        Deutsche Bahn, Trainline e Omio rispondono 403 o servono un captcha a
        chi bussa da fuori, ma la stessa identica chiamata fatta dal contesto
        della loro pagina passa: eredita cookie, token e origine, e per il
        server e' indistinguibile dal sito che chiama se stesso.

        Costa qualche secondo contro il decimo di secondo di una richiesta
        diretta, quindi si usa solo dove la via diretta e' chiusa."""
        async with self.page() as page:
            await page.goto(site_url, wait_until="domcontentloaded", timeout=45_000)
            # Le pagine protette impostano i cookie di sessione con uno script
            # che parte dopo il primo rendering, e molte sono applicazioni a
            # pagina singola che si reindirizzano subito dopo: se si valuta
            # troppo presto il contesto viene distrutto a meta' chiamata.
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001 - alcune pagine non stanno mai ferme
                pass
            await page.wait_for_timeout(wait_ms)

            script = """async ({url, method, body, headers}) => {
                    const response = await fetch(url, {
                        method,
                        headers: {'Accept': 'application/json', ...(headers || {})},
                        body: body === null ? undefined : JSON.stringify(body),
                        credentials: 'include',
                    });
                    const text = await response.text();
                    try {
                        return {ok: response.ok, status: response.status, data: JSON.parse(text)};
                    } catch (error) {
                        return {ok: false, status: response.status, text: text.slice(0, 800)};
                    }
                }"""

            payload = {
                "url": api_url,
                "method": method,
                "body": body,
                "headers": {"Content-Type": "application/json", **(headers or {})}
                if body is not None
                else (headers or {}),
            }

            try:
                return await page.evaluate(script, payload)
            except Exception as exc:  # noqa: BLE001
                if "Execution context was destroyed" not in str(exc):
                    raise
                # La pagina ha navigato mentre valutavamo: ora e' ferma, si rifa'.
                await page.wait_for_timeout(1500)
                return await page.evaluate(script, payload)

    async def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001
                pass
            self._context = None
        await self._shutdown_playwright()

    @property
    def available(self) -> bool:
        return not self._failed


_pool: BrowserPool | None = None


def get_browser_pool() -> BrowserPool:
    global _pool
    if _pool is None:
        _pool = BrowserPool()
    return _pool


async def close_browser_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
