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

# Resolve the REAL element targeted by a DOM ref or by pixel coords, and return
# ground-truth facts for the approval gate (the element's own text/role/type — never
# an opaque ref or a model-supplied label). For coordinates we hit-test with
# elementFromPoint and climb to the nearest interactive ancestor (so a click on the
# <svg> inside a <button> still resolves the button).
_DESCRIBE_JS = """
(args) => {
  const FAIL = {found:false, interactive:false, name:'', role:'', tag:'',
                href:null, input_type:null, in_form:false};
  const INTERACTIVE = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY']);
  const isInteractive = (n) => !!n && n.nodeType === 1 && (
    INTERACTIVE.has(n.tagName) ||
    (n.getAttribute && (n.getAttribute('role') === 'button' || n.getAttribute('role') === 'link')) ||
    (n.hasAttribute && n.hasAttribute('onclick')));
  let el = null;
  if (args.ref) el = document.querySelector('[data-ref="' + args.ref + '"]');
  else if (args.active) el = document.activeElement;
  else if (args.x != null && args.y != null) el = document.elementFromPoint(args.x, args.y);
  if (!el || el.nodeType !== 1) return FAIL;
  let node = el, hops = 0, target = el;
  while (node && hops < 6) { if (isInteractive(node)) { target = node; break; } node = node.parentElement; hops++; }
  const tag = (target.tagName || '').toLowerCase();
  const role = (target.getAttribute && target.getAttribute('role')) || tag;
  const name = ((target.innerText || target.value ||
                 (target.getAttribute && target.getAttribute('aria-label')) ||
                 target.placeholder || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 120);
  const href = (target.getAttribute && target.getAttribute('href')) || null;
  const input_type = (target.getAttribute && target.getAttribute('type')) || null;
  const in_form = !!(target.closest && target.closest('form'));
  return {found:true, interactive:isInteractive(target), name, role, tag, href, input_type, in_form};
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
    # sharpen-on-idle state (see _sharpen): the last 1x frame we forwarded, the
    # echo-hold window after a sharp push, the newest 1x frame held in it, and
    # the pending idle task.
    last_frame: str | None = None
    hold_until: float = 0.0
    held: str | None = None
    sharpen_task: asyncio.Task | None = None
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

    # ---- coordinate spaces ---------------------------------------------------
    # The browser viewport can be larger than what the agent sees. Everything
    # that leaves the session (screenshots, screen_size_of, frame headers) is in
    # AGENT space (`agent_image_width` px wide); everything that enters with
    # coordinates (actions, take-over input, describe) is converted back to PAGE
    # space with `_to_page`. Only possible with a CDP session (local provider);
    # otherwise the two spaces are the same.
    def _viewport_of(self, tab: _Tab) -> tuple[int, int]:
        vs = tab.page.viewport_size
        return (vs["width"], vs["height"]) if vs else (1280, 800)

    def _agent_scale(self, tab: _Tab) -> float:
        if tab.cdp is None:
            return 1.0
        w, _ = self._viewport_of(tab)
        return min(1.0, self._cfg.agent_image_width / w) if w else 1.0

    def _to_page(self, tab: _Tab, v: float | None) -> float:
        return (v or 0) / self._agent_scale(tab)

    def screen_size_of(self, tab_id: str | None = None) -> tuple[int, int]:
        """Pixel size of the tab's screenshot / agent coordinate space."""
        try:
            tab = self._tab(tab_id)
        except KeyError:
            return (1280, 800)
        w, h = self._viewport_of(tab)
        s = self._agent_scale(tab)
        return (round(w * s), round(h * s))

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
        self._cancel_sharpen(tab)
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

    async def describe_target(
        self,
        *,
        ref: str | None = None,
        x: int | None = None,
        y: int | None = None,
        active: bool = False,
        tab_id: str | None = None,
    ) -> dict:
        """Ground-truth facts about the element an action will hit — resolved from the
        live DOM by `ref` (DOM tools), by pixel coords (vision tools, via
        `elementFromPoint`), or `active` (the focused element). Returns
        `{found, interactive, name, role, tag, href, input_type, in_form}`. Best-effort:
        any error → `found=False` (the gate treats that conservatively)."""
        tab = self._tab(tab_id)
        if x is not None and y is not None:
            x, y = self._to_page(tab, x), self._to_page(tab, y)
        try:
            return await tab.page.evaluate(_DESCRIBE_JS, {"ref": ref, "x": x, "y": y, "active": active})
        except Exception:  # noqa: BLE001 — never fail an action on gate introspection
            return {
                "found": False, "interactive": False, "name": "", "role": "",
                "tag": "", "href": None, "input_type": None, "in_form": False,
            }

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
        # coordinate actions arrive in agent space
        x, y = self._to_page(tab, a.x), self._to_page(tab, a.y)
        x2, y2 = self._to_page(tab, a.x2), self._to_page(tab, a.y2)
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
            await page.mouse.click(x, y)
        elif a.kind is ActionKind.TYPE_AT:
            await page.mouse.click(x, y)
            if a.clear:
                await self._key_combination(["ControlOrMeta", "a"], tab)
                await page.keyboard.press("Delete")
            await page.keyboard.type(a.text or "")
            if a.submit:
                await page.keyboard.press("Enter")
        elif a.kind is ActionKind.SCROLL_AT:
            await page.mouse.move(x, y)
            mag = a.magnitude or 600
            dx, dy = {
                "up": (0, -mag), "down": (0, mag),
                "left": (-mag, 0), "right": (mag, 0),
            }.get(a.direction or "down", (0, mag))
            await page.mouse.wheel(dx, dy)
        elif a.kind is ActionKind.DRAG:
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.move(x2, y2)
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
        """PNG of the viewport in AGENT space (`agent_image_width` px wide), so the
        image the model sees is exactly the coordinate space it acts in."""
        tab = self._tab(tab_id)
        s = self._agent_scale(tab)
        if tab.cdp is not None and s < 1.0:
            w, h = self._viewport_of(tab)
            try:
                r = await tab.cdp.send("Page.captureScreenshot", {
                    "format": "png",
                    "clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": s},
                })
                return base64.b64decode(r["data"])
            except Exception:  # noqa: BLE001 — fall through to playwright
                pass
        return await tab.page.screenshot(scale="css")

    async def frame_jpeg_b64(self, tab_id: str | None = None) -> str | None:
        """A base64 JPEG of the tab right now — used to hand a freshly-connected
        viewer the current page immediately (the CDP screencast only emits frames
        on repaint, so a viewer of a static page would otherwise see nothing).
        Captured sharp (see _capture_sharp) since the page is static by definition."""
        tab = self._tab(tab_id)
        if tab.cdp is not None:
            data = await self._capture_sharp(tab)
            if data is not None:
                return data
        try:
            raw = await tab.page.screenshot(type="jpeg", quality=self._cfg.screencast_quality)
        except Exception:  # noqa: BLE001
            return None
        return base64.b64encode(raw).decode()

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
        data = params["data"]
        if asyncio.get_running_loop().time() < tab.hold_until:
            # echo window after a sharp push: a capture makes Chromium re-emit a
            # few 1x frames of unchanged content. Park the newest one; _sharpen
            # forwards it only if the content really changed.
            tab.held = data
            return
        await self._fanout(tab, data)
        tab.last_frame = data
        self._schedule_sharpen(tab)

    async def _fanout(self, tab: _Tab, data: str) -> None:
        for send in list(tab.subs):
            try:
                await send(data)
            except Exception:  # noqa: BLE001
                tab.subs.discard(send)

    # ---- sharpen-on-idle ----------------------------------------------------
    # The CDP screencast emits at CSS-pixel size no matter the device scale, so
    # a 1280x800 JPEG gets upscaled on the viewer's stage and looks soft. When a
    # tab stops repainting for `screencast_sharpen_delay`, push one extra frame
    # captured at `screencast_sharp_scale`x. Frame headers keep the CSS size, so
    # click mapping in the viewer is unaffected.
    _HOLD = 0.6  # seconds to swallow capture echoes after a sharp push

    def _sharpen_enabled(self, tab: _Tab) -> bool:
        return tab.cdp is not None and self._cfg.screencast_sharp_scale > 1.0

    def _schedule_sharpen(self, tab: _Tab) -> None:
        if not self._sharpen_enabled(tab) or not tab.subs:
            return
        self._cancel_sharpen(tab)
        tab.sharpen_task = asyncio.create_task(self._sharpen(tab))

    @staticmethod
    def _cancel_sharpen(tab: _Tab) -> None:
        t, tab.sharpen_task = tab.sharpen_task, None
        if t is not None and not t.done():
            t.cancel()

    async def _capture_sharp(self, tab: _Tab) -> str | None:
        """One JPEG of the viewport at the sharp scale (base64), and open the
        echo-hold window so the 1x frames the capture provokes are not forwarded."""
        w, h = self._viewport_of(tab)
        s = self._cfg
        try:
            r = await tab.cdp.send("Page.captureScreenshot", {
                "format": "jpeg", "quality": s.screencast_quality,
                "clip": {"x": 0, "y": 0, "width": w, "height": h,
                         "scale": max(1.0, s.screencast_sharp_scale)},
            })
        except Exception:  # noqa: BLE001 — navigating / closing
            return None
        tab.hold_until = asyncio.get_running_loop().time() + self._HOLD
        tab.held = None
        return r["data"]

    async def _sharpen(self, tab: _Tab) -> None:
        while True:
            await asyncio.sleep(self._cfg.screencast_sharpen_delay)
            if not tab.subs or tab.cdp is None or not tab.streaming:
                return
            data = await self._capture_sharp(tab)
            if data is None:
                return
            await self._fanout(tab, data)
            await asyncio.sleep(self._HOLD)
            tab.hold_until = 0.0
            held, tab.held = tab.held, None
            if held is None or held == tab.last_frame:
                return  # echoes only — the sharp frame stands
            # the page really changed under the hold: show it, then go again
            await self._fanout(tab, held)
            tab.last_frame = held

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
        tab = self._tab(tab_id)
        if tab.cdp:
            await tab.cdp.send(
                "Input.dispatchMouseEvent",
                {"type": type_, "x": self._to_page(tab, x), "y": self._to_page(tab, y),
                 "button": "left", "clickCount": 1},
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
        tab = self._tab(tab_id)
        if tab.cdp:
            await tab.cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": self._to_page(tab, x), "y": self._to_page(tab, y),
                 "deltaX": self._to_page(tab, dx), "deltaY": self._to_page(tab, dy)},
            )

    async def close(self) -> None:
        # Detach our connection; for browserbase the keep-alive session stays warm
        # server-side so a later ensure()/replica can reconnect by id.
        for tab in self._tabs.values():
            tab.streaming = False
            self._cancel_sharpen(tab)
        await self._ob.close()

    async def release(self) -> None:
        """Destroy the underlying browser session for good (browserbase
        REQUEST_RELEASE). For an explicit session-delete / cost-cap path — not the
        normal idle reap, which only detaches via close()."""
        for tab in self._tabs.values():
            tab.streaming = False
            self._cancel_sharpen(tab)
        if self._ob.release is not None:
            await self._ob.release()
        else:
            await self._ob.close()
