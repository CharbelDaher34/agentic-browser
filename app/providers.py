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

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable

from playwright.async_api import CDPSession, Page, async_playwright

from .config import settings
from .models import LiveViewMode, ProviderName


@dataclass
class OpenBrowser:
    page: Page
    provider: ProviderName
    live_view_mode: LiveViewMode
    cdp: CDPSession | None          # present for local (screencast + input)
    live_view_url: str | None       # present for browserbase (iframe)
    close: Callable[[], Awaitable[None]]


class BrowserProvider(ABC):
    name: ProviderName

    @abstractmethod
    async def open(self, storage_state: dict | None = None) -> OpenBrowser: ...


class LocalProvider(BrowserProvider):
    name = "local"

    async def open(self, storage_state: dict | None = None) -> OpenBrowser:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=settings().headless)
        # restoring the FULL storage_state (cookies + localStorage/origins) at
        # context creation is the only way Playwright rehydrates localStorage.
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            storage_state=storage_state or None,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        cdp = await context.new_cdp_session(page)

        async def close() -> None:
            await context.close()
            await browser.close()
            await pw.stop()

        return OpenBrowser(page, "local", "screencast", cdp, None, close)


class BrowserbaseProvider(BrowserProvider):
    name = "browserbase"

    def __init__(self, project_id: str | None = None, api_key: str | None = None) -> None:
        self._project_id = project_id or os.environ["BROWSERBASE_PROJECT_ID"]
        self._api_key = api_key or os.environ["BROWSERBASE_API_KEY"]

    async def open(self, storage_state: dict | None = None) -> OpenBrowser:
        # Browserbase manages its own session persistence server-side, so the
        # local storage_state is not replayed here.
        from browserbase import AsyncBrowserbase

        bb = AsyncBrowserbase(api_key=self._api_key)
        bb_session = await bb.sessions.create(
            project_id=self._project_id,
            keep_alive=True,                       # survive disconnects = multi-chat
            browser_settings={"recordSession": True},
        )
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(bb_session.connect_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        links = await bb.sessions.debug(bb_session.id)
        live_url = links.debugger_fullscreen_url   # embeddable iframe w/ takeover

        async def close() -> None:
            await browser.close()
            await pw.stop()
            await bb.sessions.update(
                id=bb_session.id, status="REQUEST_RELEASE", project_id=self._project_id
            )

        return OpenBrowser(page, "browserbase", "iframe", None, live_url, close)


def make_provider(name: ProviderName | None = None) -> BrowserProvider:
    name = name or settings().browser_provider  # type: ignore[assignment]
    if name == "browserbase":
        return BrowserbaseProvider()
    return LocalProvider()
