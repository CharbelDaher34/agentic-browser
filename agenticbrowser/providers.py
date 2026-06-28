"""Swappable browser backends.

Both providers hand back a connected Playwright `Page`. They differ only in how
live view + human takeover work:

  local        -> we own a CDP session: screencast frames out, Input.dispatch in
  browserbase  -> managed runtime gives an embeddable live-view iframe URL with
                  built-in takeover; the agent still drives via Playwright/CDP

The factory picks one from an env var or per-session config, so the rest of the
system never branches on the backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable

from playwright.async_api import BrowserContext, CDPSession, Page, async_playwright

from .config import CoreConfig
from .models import LiveViewMode, ProviderName

# Anti-automation hardening (adapted from computers/playwright): make the
# headless browser look like a normal Chrome so sites don't block the agent.
_STEALTH_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
]
_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
"""


@dataclass
class OpenBrowser:
    page: Page
    provider: ProviderName
    live_view_mode: LiveViewMode
    cdp: CDPSession | None          # present for local (screencast + input)
    live_view_url: str | None       # present for browserbase (iframe)
    close: Callable[[], Awaitable[None]]   # detach our connection; keep remote alive
    context: BrowserContext         # the page's context — used to open new tabs
    # browserbase session id, so a restart/replica can reconnect (None for local)
    provider_session_id: str | None = None
    # destroy the remote session for good (browserbase REQUEST_RELEASE); None = local
    release: Callable[[], Awaitable[None]] | None = None


class BrowserProvider(ABC):
    name: ProviderName

    @abstractmethod
    async def open(
        self, storage_state: dict | None = None, *, reconnect_id: str | None = None
    ) -> OpenBrowser: ...


class LocalProvider(BrowserProvider):
    name = "local"

    def __init__(self, cfg: CoreConfig) -> None:
        self._cfg = cfg

    async def open(
        self, storage_state: dict | None = None, *, reconnect_id: str | None = None
    ) -> OpenBrowser:
        # local browsers are process-local and can't be reconnected — reconnect_id
        # is accepted for interface parity and ignored.
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=self._cfg.headless, args=_STEALTH_LAUNCH_ARGS
        )
        # restoring the FULL storage_state (cookies + localStorage/origins) at
        # context creation is the only way Playwright rehydrates localStorage.
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            storage_state=storage_state or None,
            user_agent=_STEALTH_UA,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script(_STEALTH_INIT_JS)
        page = context.pages[0] if context.pages else await context.new_page()
        cdp = await context.new_cdp_session(page)

        async def close() -> None:
            await context.close()
            await browser.close()
            await pw.stop()

        return OpenBrowser(page, "local", "screencast", cdp, None, close, context)


class BrowserbaseProvider(BrowserProvider):
    name = "browserbase"

    def __init__(self, cfg: CoreConfig) -> None:
        # creds come from CoreConfig — the SDK's `browserbase=` arg, or the server's
        # .env. Nothing is read per-session.
        self._cfg = cfg
        self._project_id = cfg.browserbase_project_id
        self._api_key = cfg.browserbase_api_key

    async def open(
        self, storage_state: dict | None = None, *, reconnect_id: str | None = None
    ) -> OpenBrowser:
        if not (self._api_key and self._project_id):
            raise RuntimeError(
                "Browserbase credentials are required — add your Browserbase API "
                "key and project ID in Settings."
            )
        # Browserbase manages its own session persistence server-side, so the
        # local storage_state is not replayed here.
        from browserbase import AsyncBrowserbase

        bb = AsyncBrowserbase(api_key=self._api_key)

        # Reconnect to a still-running keep-alive session if we have its id, so a
        # restart/replica re-attaches to the same live browser instead of opening
        # a fresh one. Falls back to creating a new session if it's gone.
        bb_id: str | None = None
        connect_url: str | None = None
        if reconnect_id:
            try:
                info = await bb.sessions.retrieve(reconnect_id)
                if info.status == "RUNNING" and info.connect_url:
                    bb_id, connect_url = reconnect_id, info.connect_url
            except Exception:  # noqa: BLE001 — dead/expired/unknown -> create fresh
                bb_id = connect_url = None
        if connect_url is None:
            created = await bb.sessions.create(
                project_id=self._project_id,
                keep_alive=True,                   # survive disconnects = multi-chat
                browser_settings={
                    "recordSession": True,
                    # fingerprint/viewport hardening (from computers/browserbase)
                    "fingerprint": {
                        "screen": {
                            "maxWidth": 1920, "maxHeight": 1080,
                            "minWidth": 1024, "minHeight": 768,
                        },
                    },
                    "viewport": {"width": 1280, "height": 800},
                },
            )
            bb_id, connect_url = created.id, created.connect_url

        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(connect_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        links = await bb.sessions.debug(bb_id)
        live_url = links.debugger_fullscreen_url   # embeddable iframe w/ takeover

        async def close() -> None:
            # Detach our local Playwright connection only. The keep-alive session
            # stays warm server-side so the next ensure()/replica reconnects by id.
            await browser.close()
            await pw.stop()

        async def release() -> None:
            # Destroy the remote session for good (explicit deletion / cost cap).
            try:
                await browser.close()
                await pw.stop()
            finally:
                await bb.sessions.update(
                    id=bb_id, status="REQUEST_RELEASE", project_id=self._project_id
                )

        return OpenBrowser(
            page, "browserbase", "iframe", None, live_url, close, context,
            provider_session_id=bb_id, release=release,
        )


def make_provider(name: ProviderName | None = None, *, cfg: CoreConfig) -> BrowserProvider:
    name = name or cfg.browser_provider  # type: ignore[assignment]
    if name == "browserbase":
        # creds come from CoreConfig (SDK `browserbase=`, or the server's .env)
        return BrowserbaseProvider(cfg)
    return LocalProvider(cfg)
