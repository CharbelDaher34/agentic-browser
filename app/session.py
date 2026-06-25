"""PlaywrightSession — provider-agnostic page wrapper.

Perception + act/verify are identical across providers. Live view diverges:
screencast (local) fans JPEG frames to subscribers and injects input via CDP;
iframe (browserbase) just exposes a URL the frontend embeds.
"""

from __future__ import annotations

import hashlib
from typing import Awaitable, Callable

from .models import Action, ActionKind, ActionResult, Element, PageObservation
from .providers import OpenBrowser

_COLLECT_JS = """
() => {
  const sel = 'a,button,input,select,textarea,[role=button],[onclick]';
  const out = [];
  document.querySelectorAll(sel).forEach((el, i) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const ref = 'e' + i;
    el.setAttribute('data-ref', ref);
    out.push({ref,
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      name: (el.innerText || el.value || el.getAttribute('aria-label')
             || el.placeholder || '').trim().slice(0, 80),
      value: el.value || null, enabled: !el.disabled});
  });
  return { elements: out, text: document.body.innerText.slice(0, 4000) };
}
"""


class PlaywrightSession:
    def __init__(self, ob: OpenBrowser) -> None:
        self._ob = ob
        self._page = ob.page
        self._cdp = ob.cdp
        self._subs: set[Callable[[str], Awaitable[None]]] = set()
        self._streaming = False

    @property
    def live_view_mode(self) -> str:
        return self._ob.live_view_mode

    @property
    def live_view_url(self) -> str | None:
        return self._ob.live_view_url

    @classmethod
    async def open(cls, provider, storage_state: dict | None = None) -> "PlaywrightSession":
        self = cls(await provider.open(storage_state))
        if self._cdp is not None:
            await self._start_screencast()
        return self

    # ---- perception ---------------------------------------------------------
    async def observe(self) -> PageObservation:
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001 — networkidle can time out on live pages
            pass
        snap = await self._page.evaluate(_COLLECT_JS)
        elements = [Element(**e) for e in snap["elements"]]
        url = self._page.url
        fp = hashlib.sha1(
            (url + "|".join(e.ref + e.name for e in elements)).encode()
        ).hexdigest()[:12]
        return PageObservation(
            url=url,
            title=await self._page.title(),
            elements=elements,
            text_digest=snap["text"],
            fingerprint=fp,
        )

    # ---- act + verify -------------------------------------------------------
    async def dispatch(self, action: Action, before: PageObservation) -> ActionResult:
        try:
            await self._perform(action)
        except Exception as exc:  # noqa: BLE001
            after = await self.observe()
            return ActionResult(
                False, after.fingerprint != before.fingerprint, after, str(exc)
            )
        after = await self.observe()
        return ActionResult(True, after.fingerprint != before.fingerprint, after)

    async def _perform(self, a: Action) -> None:
        if a.kind is ActionKind.NAVIGATE and a.url:
            await self._page.goto(a.url)
        elif a.kind is ActionKind.CLICK and a.ref:
            await self._page.click(f"[data-ref='{a.ref}']")
        elif a.kind is ActionKind.TYPE and a.ref is not None:
            loc = self._page.locator(f"[data-ref='{a.ref}']")
            await loc.fill(a.text or "")
            if a.submit:
                await loc.press("Enter")
        elif a.kind is ActionKind.SELECT and a.ref:
            await self._page.select_option(f"[data-ref='{a.ref}']", a.text or "")
        elif a.kind is ActionKind.SCROLL:
            await self._page.mouse.wheel(0, 600)

    async def screenshot(self) -> bytes:
        return await self._page.screenshot()

    async def storage_state(self) -> dict:
        return await self._page.context.storage_state()

    # ---- screencast (local only) -------------------------------------------
    async def _start_screencast(self) -> None:
        if self._streaming or self._cdp is None:
            return
        self._streaming = True
        self._cdp.on("Page.screencastFrame", self._on_frame)
        await self._cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 60, "everyNthFrame": 1},
        )

    async def _on_frame(self, params: dict) -> None:
        await self._cdp.send(
            "Page.screencastFrameAck", {"sessionId": params["sessionId"]}
        )
        for send in list(self._subs):
            try:
                await send(params["data"])
            except Exception:  # noqa: BLE001
                self._subs.discard(send)

    def subscribe(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._subs.add(send)

    def unsubscribe(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._subs.discard(send)

    # ---- input injection (local only; browserbase takeover is in the iframe)
    async def inject_mouse(self, x: float, y: float, type_: str = "mousePressed") -> None:
        if self._cdp:
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {"type": type_, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    async def inject_key(self, key: str, text: str | None = None) -> None:
        if self._cdp:
            await self._cdp.send(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "key": key, "text": text or ""},
            )
            await self._cdp.send(
                "Input.dispatchKeyEvent", {"type": "keyUp", "key": key}
            )

    async def close(self) -> None:
        self._streaming = False
        await self._ob.close()
