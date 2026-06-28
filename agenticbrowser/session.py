"""PlaywrightSession — provider-agnostic, multi-tab page wrapper.

Perception + act/verify are identical across providers. Live view diverges:
screencast (local) fans JPEG frames to subscribers and injects input via CDP;
iframe (browserbase) just exposes a URL the frontend embeds.

A session owns one browser *context* and one or more *tabs* (`_Tab`). The first
tab is the "primary" (`t0`) and every public method defaults to it, so callers
that don't care about tabs behave exactly as the old single-tab session did.
Sub-agents drive their own tabs by passing an explicit `tab_id`.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .config import CoreConfig
from .models import Action, ActionKind, ActionResult, Element, PageObservation
from .providers import OpenBrowser

# user-friendly key names -> Playwright key names (from computers/playwright)
_KEY_MAP = {
    "enter": "Enter", "return": "Enter", "tab": "Tab", "backspace": "Backspace",
    "delete": "Delete", "escape": "Escape", "esc": "Escape", "space": "Space",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End",
    "control": "ControlOrMeta", "ctrl": "ControlOrMeta", "cmd": "Meta",
    "command": "Meta", "shift": "Shift", "alt": "Alt", "meta": "Meta",
}


def _norm_key(k: str) -> str:
    return _KEY_MAP.get(k.strip().lower(), k.strip())


# Virtual-key codes for non-printable keys, by the DOM `key` value the frontend
# sends. CDP needs these for editing/navigation keys to take effect on takeover.
_VK_CODES = {
    "Backspace": 8, "Tab": 9, "Enter": 13, "Shift": 16, "Control": 17, "Alt": 18,
    "Escape": 27, "Space": 32, " ": 32, "PageUp": 33, "PageDown": 34,
    "End": 35, "Home": 36, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39,
    "ArrowDown": 40, "Insert": 45, "Delete": 46, "Meta": 91,
}

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


@dataclass
class _Tab:
    """One browser tab: its page, optional CDP session, and screencast state."""
    tab_id: str
    page: object                      # playwright Page
    cdp: object | None                # CDPSession (local) or None (browserbase)
    label: str = ""
    streaming: bool = False
    subs: set = field(default_factory=set)
    # serializes an action's perform+observe against the popup adopt path so the
    # page isn't mutated mid-flight. observe() must NOT take this lock —
    # dispatch() holds it while calling observe(), and asyncio.Lock isn't reentrant.
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PlaywrightSession:
    def __init__(self, ob: OpenBrowser, cfg: CoreConfig) -> None:
        self._ob = ob
        self._cfg = cfg
        self._context = ob.context
        self._tabs: dict[str, _Tab] = {}
        self._primary = "t0"
        self._tab_seq = 0
        # detached tasks (popup adoption) we may need to cancel on interrupt
        self._bg_tasks: set[asyncio.Task] = set()

    # ---- tab plumbing -------------------------------------------------------
    def _next_tab_id(self) -> str:
        self._tab_seq += 1
        return f"t{self._tab_seq}"

    def _tab(self, tab_id: str | None) -> _Tab:
        return self._tabs[tab_id or self._primary]

    def has_tab(self, tab_id: str) -> bool:
        return tab_id in self._tabs

    async def _new_cdp(self, page) -> object | None:
        # local owns CDP (screencast + input); browserbase has no CDP (iframe).
        if self._ob.cdp is None:
            return None
        try:
            return await self._context.new_cdp_session(page)
        except Exception:  # noqa: BLE001
            return None

    @property
    def live_view_mode(self) -> str:
        return self._ob.live_view_mode

    @property
    def live_view_url(self) -> str | None:
        return self._ob.live_view_url

    @property
    def provider_session_id(self) -> str | None:
        """browserbase session id (for persist + reconnect); None for local."""
        return self._ob.provider_session_id

    @property
    def _page(self):
        """Primary page — compat shim for callers that reach in directly."""
        return self._tab(None).page

    def screen_size_of(self, tab_id: str | None = None) -> tuple[int, int]:
        """Pixel size of the tab's screenshot / coordinate space."""
        try:
            vs = self._tab(tab_id).page.viewport_size
        except KeyError:
            vs = None
        return (vs["width"], vs["height"]) if vs else (1280, 800)

    @property
    def screen_size(self) -> tuple[int, int]:
        return self.screen_size_of(None)

    def url_of(self, tab_id: str | None = None) -> str:
        try:
            return self._tab(tab_id).page.url
        except KeyError:
            return ""

    @property
    def url(self) -> str:
        return self.url_of(None)

    @classmethod
    async def open(
        cls, provider, storage_state: dict | None = None, *,
        cfg: CoreConfig, reconnect_id: str | None = None,
    ) -> "PlaywrightSession":
        self = cls(await provider.open(storage_state, reconnect_id=reconnect_id), cfg)
        t0 = _Tab(self._primary, self._ob.page, self._ob.cdp, label="main")
        self._tabs[self._primary] = t0
        # New tabs/popups are ADOPTED as real tabs (multi-tab model). Listening at
        # the context level catches both agent-opened pages and site popups.
        self._context.on("page", self._on_popup)
        if t0.cdp is not None:
            await self._start_screencast(t0)
        return self

    # ---- tab lifecycle ------------------------------------------------------
    async def open_tab(self, url: str | None = None, label: str = "") -> str:
        page = await self._context.new_page()
        # Register the tab SYNCHRONOUSLY (before any await) so the context "page"
        # event handler (_on_popup) recognises it as one of ours and never closes
        # it as a stray about:blank popup. cdp/screencast are attached after.
        tab_id = self._next_tab_id()
        tab = _Tab(tab_id, page, None, label=label)
        self._tabs[tab_id] = tab
        tab.cdp = await self._new_cdp(page)
        if tab.cdp is not None:
            await self._start_screencast(tab)
        if url:
            try:
                await page.goto(url)
            except Exception:  # noqa: BLE001
                pass
        return tab_id

    def list_tabs(self) -> list[dict]:
        out = []
        for t in self._tabs.values():
            out.append({
                "tab_id": t.tab_id,
                "url": t.page.url,
                "label": t.label,
                "primary": t.tab_id == self._primary,
            })
        return out

    async def close_tab(self, tab_id: str) -> None:
        if tab_id == self._primary or tab_id not in self._tabs:
            return  # never close the primary tab
        tab = self._tabs.pop(tab_id)
        tab.streaming = False
        try:
            await tab.page.close()
        except Exception:  # noqa: BLE001
            pass

    def _on_popup(self, page) -> None:
        async def _adopt() -> None:
            # Only manage GENUINE popups — pages opened *by* another page, which
            # have an opener. Pages we create ourselves (open_tab / new_page) have
            # no opener; touching them here would race-close a sub-agent's tab.
            try:
                opener = await page.opener()
            except Exception:  # noqa: BLE001
                opener = None
            if opener is None:
                return
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            if any(t.page is page for t in self._tabs.values()):
                return
            url = page.url
            # Adopt genuine http(s) popups as real tabs. about:blank / other schemes
            # are usually opener handshakes (OAuth/login talk back via window.opener);
            # close the stray tab so the flow completes on the opener.
            if not (url and url.startswith(("http://", "https://"))):
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            tab_id = self._next_tab_id()
            tab = _Tab(tab_id, page, None, label="popup")
            self._tabs[tab_id] = tab
            tab.cdp = await self._new_cdp(page)
            if tab.cdp is not None:
                await self._start_screencast(tab)

        try:
            t = asyncio.get_running_loop().create_task(_adopt())
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:  # no running loop (shouldn't happen in async ctx)
            pass

    async def cancel_background(self) -> None:
        """Cancel detached popup-adoption tasks (e.g. on interrupt) and await."""
        tasks = list(self._bg_tasks)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ---- perception ---------------------------------------------------------
    async def observe(self, tab_id: str | None = None) -> PageObservation:
        page = self._tab(tab_id).page
        # An action may trigger a navigation, so evaluate() can hit "Execution
        # context was destroyed". Wait for load state and retry a few times.
        snap = {"elements": [], "text": ""}
        for _ in range(4):
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 — networkidle can time out on live pages
                pass
            try:
                snap = await page.evaluate(_COLLECT_JS)
                break
            except Exception as exc:  # noqa: BLE001
                if "context was destroyed" in str(exc) or "navigation" in str(exc).lower():
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(0.3)
                    continue
                raise
        elements = [Element(**e) for e in snap["elements"]]
        url = page.url
        fp = hashlib.sha1(
            (url + "|".join(e.ref + e.name for e in elements)).encode()
        ).hexdigest()[:12]
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001
            title = ""
        return PageObservation(
            url=url,
            title=title,
            elements=elements,
            text_digest=snap["text"],
            fingerprint=fp,
        )

    # ---- act + verify -------------------------------------------------------
    async def dispatch(
        self, action: Action, before: PageObservation, tab_id: str | None = None
    ) -> ActionResult:
        tab = self._tab(tab_id)
        # hold the tab lock across perform+observe so a popup adopt / concurrent
        # action waits until this one settles instead of racing the page.
        async with tab.action_lock:
            try:
                await self._perform(action, tab)
            except Exception as exc:  # noqa: BLE001
                after = await self.observe(tab.tab_id)
                return ActionResult(
                    False, after.fingerprint != before.fingerprint, after, str(exc)
                )
            after = await self.observe(tab.tab_id)
            return ActionResult(True, after.fingerprint != before.fingerprint, after)

    async def _perform(self, a: Action, tab: _Tab) -> None:
        page = tab.page
        # ---- DOM-ref based ----
        if a.kind is ActionKind.NAVIGATE and a.url:
            await page.goto(a.url)
        elif a.kind is ActionKind.CLICK and a.ref:
            await page.click(f"[data-ref='{a.ref}']")
        elif a.kind is ActionKind.TYPE and a.ref is not None:
            loc = page.locator(f"[data-ref='{a.ref}']")
            await loc.fill(a.text or "")
            if a.submit:
                await loc.press("Enter")
        elif a.kind is ActionKind.SELECT and a.ref:
            await page.select_option(f"[data-ref='{a.ref}']", a.text or "")
        elif a.kind is ActionKind.SCROLL:
            await page.mouse.wheel(0, 600)
        # ---- vision / coordinate based ----
        elif a.kind is ActionKind.CLICK_AT:
            await page.mouse.click(a.x or 0, a.y or 0)
        elif a.kind is ActionKind.TYPE_AT:
            await page.mouse.click(a.x or 0, a.y or 0)
            if a.clear:
                await self._key_combination(["ControlOrMeta", "a"], tab)
                await page.keyboard.press("Delete")
            await page.keyboard.type(a.text or "")
            if a.submit:
                await page.keyboard.press("Enter")
        elif a.kind is ActionKind.SCROLL_AT:
            await page.mouse.move(a.x or 0, a.y or 0)
            mag = a.magnitude or 600
            dx, dy = {
                "up": (0, -mag), "down": (0, mag),
                "left": (-mag, 0), "right": (mag, 0),
            }.get(a.direction or "down", (0, mag))
            await page.mouse.wheel(dx, dy)
        elif a.kind is ActionKind.DRAG:
            await page.mouse.move(a.x or 0, a.y or 0)
            await page.mouse.down()
            await page.mouse.move(a.x2 or 0, a.y2 or 0)
            await page.mouse.up()
        elif a.kind is ActionKind.KEY and a.keys:
            await self._key_combination([p for p in a.keys.split("+") if p], tab)
        elif a.kind is ActionKind.BACK:
            await page.go_back()
        elif a.kind is ActionKind.FORWARD:
            await page.go_forward()
        elif a.kind is ActionKind.WAIT:
            await asyncio.sleep(min(a.seconds or 3.0, 15.0))

    async def _key_combination(self, keys: list[str], tab: _Tab) -> None:
        keys = [_norm_key(k) for k in keys]
        kb = tab.page.keyboard
        for key in keys[:-1]:
            await kb.down(key)
        await kb.press(keys[-1])
        for key in reversed(keys[:-1]):
            await kb.up(key)

    async def goto(self, url: str, tab_id: str | None = None) -> None:
        """Navigate a tab directly (used to restore the last URL on rehydrate)."""
        try:
            await self._tab(tab_id).page.goto(url)
        except Exception:  # noqa: BLE001
            pass

    async def screenshot(self, tab_id: str | None = None) -> bytes:
        return await self._tab(tab_id).page.screenshot()

    async def frame_jpeg_b64(self, tab_id: str | None = None) -> str | None:
        """A base64 JPEG of the tab right now — used to hand a freshly-connected
        viewer the current page immediately (the CDP screencast only emits frames
        on repaint, so a viewer of a static page would otherwise see nothing)."""
        try:
            data = await self._tab(tab_id).page.screenshot(
                type="jpeg", quality=self._cfg.screencast_quality
            )
        except Exception:  # noqa: BLE001
            return None
        return base64.b64encode(data).decode()

    async def storage_state(self) -> dict:
        # context-level: cookies + localStorage shared across all tabs
        return await self._context.storage_state()

    # ---- screencast (local only) -------------------------------------------
    async def _start_screencast(self, tab: _Tab) -> None:
        if tab.streaming or tab.cdp is None:
            return
        tab.streaming = True
        s = self._cfg
        params: dict = {
            "format": "jpeg",
            "quality": s.screencast_quality,
            "everyNthFrame": max(1, s.screencast_every_nth_frame),
        }
        if s.screencast_max_width:
            params["maxWidth"] = s.screencast_max_width
        if s.screencast_max_height:
            params["maxHeight"] = s.screencast_max_height
        tab.cdp.on("Page.screencastFrame", functools.partial(self._on_frame, tab=tab))
        await tab.cdp.send("Page.startScreencast", params)

    async def _on_frame(self, params: dict, tab: _Tab) -> None:
        if tab.cdp is None:
            return
        try:
            await tab.cdp.send(
                "Page.screencastFrameAck", {"sessionId": params["sessionId"]}
            )
        except Exception:  # noqa: BLE001 — tab may be closing
            return
        for send in list(tab.subs):
            try:
                await send(params["data"])
            except Exception:  # noqa: BLE001
                tab.subs.discard(send)

    def subscribe(self, send: Callable[[str], Awaitable[None]], tab_id: str | None = None) -> None:
        self._tab(tab_id).subs.add(send)

    def unsubscribe(self, send: Callable[[str], Awaitable[None]], tab_id: str | None = None) -> None:
        try:
            self._tab(tab_id).subs.discard(send)
        except KeyError:
            pass

    # ---- input injection (local only; browserbase takeover is in the iframe)
    async def inject_mouse(
        self, x: float, y: float, type_: str = "mousePressed", tab_id: str | None = None
    ) -> None:
        cdp = self._tab(tab_id).cdp
        if cdp:
            await cdp.send(
                "Input.dispatchMouseEvent",
                {"type": type_, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    async def inject_key(
        self, key: str, text: str | None = None, tab_id: str | None = None
    ) -> None:
        cdp = self._tab(tab_id).cdp
        if not cdp:
            return
        # Non-printable keys (Backspace/Delete/Enter/arrows/…) need a virtual key
        # code — `key` alone does NOT trigger the edit/navigation in CDP. Printable
        # keys go through `text`. (Mirrors Puppeteer's keyboard dispatch.)
        vk = _VK_CODES.get(key)
        down: dict = {"type": "keyDown" if text else "rawKeyDown", "key": key}
        up: dict = {"type": "keyUp", "key": key}
        if text:
            down["text"] = text
        if vk is not None:
            for ev in (down, up):
                ev["windowsVirtualKeyCode"] = vk
                ev["nativeVirtualKeyCode"] = vk
                ev["code"] = key
        await cdp.send("Input.dispatchKeyEvent", down)
        await cdp.send("Input.dispatchKeyEvent", up)

    async def inject_scroll(
        self, x: float, y: float, dx: float, dy: float, tab_id: str | None = None
    ) -> None:
        cdp = self._tab(tab_id).cdp
        if cdp:
            await cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy},
            )

    async def close(self) -> None:
        # Detach our connection; for browserbase the keep-alive session stays warm
        # server-side so a later ensure()/replica can reconnect by id.
        for tab in self._tabs.values():
            tab.streaming = False
        await self._ob.close()

    async def release(self) -> None:
        """Destroy the underlying browser session for good (browserbase
        REQUEST_RELEASE). For an explicit session-delete / cost-cap path — not the
        normal idle reap, which only detaches via close()."""
        for tab in self._tabs.values():
            tab.streaming = False
        if self._ob.release is not None:
            await self._ob.release()
        else:
            await self._ob.close()
