# Agentic browser chatbot — backend (v2)

FastAPI + **PydanticAI v2** + Playwright, with everything from the requirements
wired in: Postgres persistence, full step recording, an eval suite, a swappable
local/Browserbase backend, and live streaming of model tokens *and* steps during
a session.

What changed from the first skeleton:

- **PydanticAI v2.** v2.0.0 went stable on 2026-06-23. The `Agent` + `@tool` API
  used here is unchanged in v2 (the new "capabilities" primitive is additive);
  the streaming path uses v2's `run_stream_events`. Pin `pydantic-ai>=2,<3`.
- **Streaming tokens + steps.** `runner.py` iterates `run_stream_events` and
  pushes `token` deltas, `thinking`, `tool_call`/`tool_result`, plus the
  tool-emitted `action`/`observation` step events — all live over the chat WS.
- **Postgres.** `storage_state` (auth), chat→session bindings, message history,
  and every step row, in `store.py`.
- **Recorder.** `recorder.py` writes a screenshot artifact + a `steps` row per
  action — the replay trail.
- **Eval suite.** `evals.py` uses `pydantic_evals` (Dataset/Case/Evaluator).
- **Swappable backend.** `providers.py` — `local` (own CDP: screencast +
  input injection) or `browserbase` (managed iframe live view with built-in
  takeover). One env var flips it; nothing else branches.

Mapping to `protocols.py`: `Policy` -> `agent`; `BrowserSession`+`Perceiver`+
`ActionExecutor` -> `PlaywrightSession`; `Recorder` -> `recorder.py`; `Memory`
-> `store.py` (message history + storage_state); `Guard`+`HumanChannel` ->
approval gate + lease/takeover; `EvalRunner` -> `evals.py`.

## Project layout

```
app/
  models.py     value types + provider/live-view modes + StepRecord
  providers.py  local Playwright vs Browserbase, behind one interface
  session.py    PlaywrightSession: perceive, act, live view (screencast|iframe)
  store.py      Postgres: storage_state, chats, message history, steps
  recorder.py   per-step screenshot + DB row (replay trail)
  registry.py   named sessions + storage restore + control lease
  agent.py      PydanticAI v2 agent + tools (record + emit + approval gate)
  runner.py     run_stream_events loop: tokens + steps + approvals + persist
  evals.py      pydantic_evals dataset + success evaluators
  gateway.py    FastAPI: REST + streaming chat WS + view/takeover WS
```

## Install

```bash
uv add "pydantic-ai-slim[anthropic]>=2,<3" "pydantic-evals>=2,<3" \
       playwright "fastapi" "uvicorn[standard]" asyncpg --prerelease=allow browserbase
uv run playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL=postgresql://user:pass@localhost/agent
export BROWSER_PROVIDER=local            # or: browserbase
# if browserbase:
export BROWSERBASE_API_KEY=bb_...  BROWSERBASE_PROJECT_ID=proj_...
```


## `models.py`

Value types — adds provider/live-view modes and the `StepRecord` the recorder persists.

```python
"""Value types. Same vocabulary as protocols.py, plus the provider/live-view
modes and the StepRecord the recorder persists."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, Sequence


class Risk(str, Enum):
    SAFE = "safe"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    EXTRACT = "extract"


@dataclass(frozen=True, slots=True)
class Element:
    ref: str
    role: str
    name: str
    value: str | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PageObservation:
    url: str
    title: str
    elements: Sequence[Element]
    text_digest: str
    fingerprint: str

    def to_json(self) -> dict:
        return {"url": self.url, "title": self.title, "fingerprint": self.fingerprint,
                "elements": [e.__dict__ for e in self.elements]}


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    risk: Risk = Risk.SAFE
    ref: str | None = None
    text: str | None = None
    url: str | None = None
    submit: bool = False

    def to_json(self) -> dict:
        return {"kind": self.kind.value, "risk": self.risk.value, "ref": self.ref,
                "text": self.text, "url": self.url}


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    changed: bool
    observation: PageObservation
    error: str | None = None


# --- live control plane ------------------------------------------------------

ProviderName = Literal["local", "browserbase"]
DriverKind = Literal["agent", "human", "none"]
LiveViewMode = Literal["screencast", "iframe"]


@dataclass(frozen=True, slots=True)
class Lease:
    driver: DriverKind
    holder_id: str
    token: str


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One persisted step — observation + action + result + screenshot pointer."""

    chat_id: str
    session_id: str
    idx: int
    action: Action
    result: ActionResult
    screenshot_uri: str | None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    type: Literal["token", "thinking", "tool_call", "tool_result", "action",
                  "observation", "approval_request", "final", "error", "lease",
                  "live_view"]
    chat_id: str
    data: Mapping[str, Any] = field(default_factory=dict)
```

## `providers.py`

Swappable backends behind one interface: local Playwright (we own CDP) vs Browserbase (managed, iframe live view). The factory reads `BROWSER_PROVIDER`.

```python
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
    async def open(self) -> OpenBrowser: ...


class LocalProvider(BrowserProvider):
    name = "local"

    async def open(self) -> OpenBrowser:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
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

    async def open(self) -> OpenBrowser:
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
            await bb.sessions.update(id=bb_session.id, status="REQUEST_RELEASE",
                                     project_id=self._project_id)

        return OpenBrowser(page, "browserbase", "iframe", None, live_url, close)


def make_provider(name: ProviderName | None = None) -> BrowserProvider:
    name = name or os.environ.get("BROWSER_PROVIDER", "local")  # type: ignore[assignment]
    if name == "browserbase":
        return BrowserbaseProvider()
    return LocalProvider()
```

## `session.py`

`PlaywrightSession` — provider-agnostic perception + act/verify; live view is screencast (local) or an iframe URL (browserbase).

```python
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
    async def open(cls, provider) -> "PlaywrightSession":
        self = cls(await provider.open())
        if self._cdp is not None:
            await self._start_screencast()
        return self

    # ---- perception ---------------------------------------------------------
    async def observe(self) -> PageObservation:
        await self._page.wait_for_load_state("networkidle")
        snap = await self._page.evaluate(_COLLECT_JS)
        elements = [Element(**e) for e in snap["elements"]]
        url = self._page.url
        fp = hashlib.sha1((url + "|".join(e.ref + e.name for e in elements))
                          .encode()).hexdigest()[:12]
        return PageObservation(url=url, title=await self._page.title(),
                               elements=elements, text_digest=snap["text"],
                               fingerprint=fp)

    # ---- act + verify -------------------------------------------------------
    async def dispatch(self, action: Action, before: PageObservation) -> ActionResult:
        try:
            await self._perform(action)
        except Exception as exc:  # noqa: BLE001
            after = await self.observe()
            return ActionResult(False, after.fingerprint != before.fingerprint,
                                after, str(exc))
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
        await self._cdp.send("Page.startScreencast",
                             {"format": "jpeg", "quality": 60, "everyNthFrame": 1})

    async def _on_frame(self, params: dict) -> None:
        await self._cdp.send("Page.screencastFrameAck",
                             {"sessionId": params["sessionId"]})
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
            await self._cdp.send("Input.dispatchMouseEvent",
                                 {"type": type_, "x": x, "y": y, "button": "left",
                                  "clickCount": 1})

    async def inject_key(self, key: str, text: str | None = None) -> None:
        if self._cdp:
            await self._cdp.send("Input.dispatchKeyEvent",
                                 {"type": "keyDown", "key": key, "text": text or ""})
            await self._cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})

    async def close(self) -> None:
        self._streaming = False
        await self._ob.close()
```

## `store.py`

Postgres (asyncpg). Persists `storage_state`, chat→session bindings, PydanticAI message history (via the v2 `ModelMessagesTypeAdapter`), and every step.

```python
"""Postgres persistence (asyncpg).

Four tables:
  sessions  — provider + latest storage_state (cookies/auth) per named session
  chats     — chat -> session binding
  messages  — PydanticAI message history per chat, serialized with the v2
              ModelMessagesTypeAdapter so a chat resumes with full context
  steps     — every agent step (action + result + screenshot pointer): the
              Recorder's sink and the replay trail
"""

from __future__ import annotations

import json

import asyncpg
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from .models import StepRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  provider     TEXT NOT NULL,
  storage_state JSONB,
  updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chats (
  chat_id    TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(session_id),
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
  chat_id TEXT PRIMARY KEY REFERENCES chats(chat_id),
  blob    BYTEA NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS steps (
  id          BIGSERIAL PRIMARY KEY,
  chat_id     TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  idx         INT NOT NULL,
  action      JSONB NOT NULL,
  result      JSONB NOT NULL,
  screenshot_uri TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS steps_chat_idx ON steps(chat_id, idx);
"""


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Store":
        pool = await asyncpg.create_pool(dsn)
        async with pool.acquire() as c:
            await c.execute(SCHEMA)
        return cls(pool)

    # ---- sessions -----------------------------------------------------------
    async def upsert_session(self, session_id: str, provider: str) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO sessions(session_id, provider) VALUES($1,$2) "
                "ON CONFLICT (session_id) DO NOTHING", session_id, provider)

    async def save_storage_state(self, session_id: str, state: dict) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "UPDATE sessions SET storage_state=$2, updated_at=now() "
                "WHERE session_id=$1", session_id, json.dumps(state))

    async def load_storage_state(self, session_id: str) -> dict | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT storage_state FROM sessions WHERE session_id=$1", session_id)
        return json.loads(row["storage_state"]) if row and row["storage_state"] else None

    # ---- chats --------------------------------------------------------------
    async def bind_chat(self, chat_id: str, session_id: str) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO chats(chat_id, session_id) VALUES($1,$2) "
                "ON CONFLICT (chat_id) DO UPDATE SET session_id=$2",
                chat_id, session_id)

    async def session_of(self, chat_id: str) -> str | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT session_id FROM chats WHERE chat_id=$1", chat_id)
        return row["session_id"] if row else None

    # ---- message history (PydanticAI) --------------------------------------
    async def save_messages(self, chat_id: str, messages: list[ModelMessage]) -> None:
        blob = ModelMessagesTypeAdapter.dump_json(messages)   # bytes
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO messages(chat_id, blob) VALUES($1,$2) "
                "ON CONFLICT (chat_id) DO UPDATE SET blob=$2, updated_at=now()",
                chat_id, blob)

    async def load_messages(self, chat_id: str) -> list[ModelMessage]:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT blob FROM messages WHERE chat_id=$1", chat_id)
        if not row:
            return []
        return ModelMessagesTypeAdapter.validate_json(row["blob"])

    # ---- steps (Recorder sink) ---------------------------------------------
    async def insert_step(self, s: StepRecord) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO steps(chat_id, session_id, idx, action, result, "
                "screenshot_uri) VALUES($1,$2,$3,$4,$5,$6)",
                s.chat_id, s.session_id, s.idx,
                json.dumps(s.action.to_json()),
                json.dumps({"ok": s.result.ok, "changed": s.result.changed,
                            "error": s.result.error,
                            "observation": s.result.observation.to_json()}),
                s.screenshot_uri)
```

## `recorder.py`

`Recorder` + artifact store — each step becomes a row plus a screenshot artifact; together with message history that's your replay trail.

```python
"""Recorder — turns each agent step into a durable, replayable record.

Screenshots go to an artifact store (local disk here; swap for S3/GCS in prod);
the row in `steps` keeps the action, result, observation, and the artifact URI.
Together with the message history this is your full replay trail.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol

from .models import Action, ActionResult, StepRecord
from .store import Store


class ArtifactStore(Protocol):
    async def put_png(self, key: str, data: bytes) -> str: ...


class LocalArtifacts:
    def __init__(self, root: str = "/var/lib/agent/artifacts") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    async def put_png(self, key: str, data: bytes) -> str:
        path = self._root / f"{key}.png"
        path.write_bytes(data)
        return path.as_uri()


class Recorder:
    def __init__(self, store: Store, artifacts: ArtifactStore) -> None:
        self._store = store
        self._artifacts = artifacts

    async def record(self, chat_id: str, session_id: str, idx: int,
                     action: Action, result: ActionResult,
                     screenshot: bytes | None) -> None:
        uri = None
        if screenshot is not None:
            key = f"{chat_id}/{idx}-{uuid.uuid4().hex[:8]}"
            uri = await self._artifacts.put_png(key, screenshot)
        await self._store.insert_step(StepRecord(
            chat_id=chat_id, session_id=session_id, idx=idx,
            action=action, result=result, screenshot_uri=uri))
```

## `registry.py`

`SessionRegistry` — provider-backed named sessions, storage_state restore on create, and the single-driver control lease.

```python
"""SessionRegistry — named, long-lived sessions + the single-driver lease.

Sessions outlive chats. On create we restore persisted storage_state so auth
survives restarts. The lease still guarantees one driver; takeover clears the
agent_may_drive event so in-flight agent tools block instead of racing a human.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from .models import DriverKind, Lease, ProviderName
from .providers import make_provider
from .session import PlaywrightSession
from .store import Store

IDLE_TTL_SECONDS = 30 * 60


@dataclass
class _Entry:
    session: PlaywrightSession
    provider: ProviderName
    lease: Lease
    chats: set[str] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)
    agent_may_drive: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionRegistry:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._sessions: dict[str, _Entry] = {}

    async def create(self, session_id: str,
                     provider_name: ProviderName | None = None) -> str:
        if session_id in self._sessions:
            return session_id
        provider = make_provider(provider_name)
        session = await PlaywrightSession.open(provider)
        # restore cookies/auth from a previous run if present
        prior = await self._store.load_storage_state(session_id)
        if prior:
            try:
                await session._page.context.add_cookies(prior.get("cookies", []))
            except Exception:  # noqa: BLE001
                pass
        await self._store.upsert_session(session_id, provider.name)
        self._sessions[session_id] = _Entry(
            session=session, provider=provider.name,
            lease=Lease("none", "", ""))
        return session_id

    def get(self, session_id: str) -> PlaywrightSession:
        return self._sessions[session_id].session

    def attach(self, session_id: str, chat_id: str) -> PlaywrightSession:
        e = self._sessions[session_id]
        e.chats.add(chat_id)
        e.last_used = time.monotonic()
        return e.session

    def detach(self, session_id: str, chat_id: str) -> None:
        e = self._sessions.get(session_id)
        if e:
            e.chats.discard(chat_id)

    # ---- lease --------------------------------------------------------------
    async def acquire(self, session_id: str, driver: DriverKind,
                      holder_id: str) -> Lease | None:
        e = self._sessions[session_id]
        async with e.lock:
            cur = e.lease
            if cur.driver != "none" and cur.holder_id != holder_id:
                return None
            e.lease = Lease(driver, holder_id, secrets.token_hex(8))
            (e.agent_may_drive.set if driver == "agent" else e.agent_may_drive.clear)()
            return e.lease

    async def release(self, session_id: str, token: str) -> None:
        e = self._sessions[session_id]
        async with e.lock:
            if e.lease.token == token:
                e.lease = Lease("none", "", "")
                e.agent_may_drive.clear()

    def holds(self, session_id: str, token: str) -> bool:
        return self._sessions[session_id].lease.token == token

    async def wait_until_agent_may_drive(self, session_id: str) -> None:
        await self._sessions[session_id].agent_may_drive.wait()

    async def reap_idle(self) -> None:
        now = time.monotonic()
        for sid, e in list(self._sessions.items()):
            if not e.chats and now - e.last_used > IDLE_TTL_SECONDS:
                await self._store.save_storage_state(sid, await e.session.storage_state())
                await e.session.close()
                del self._sessions[sid]
```

## `agent.py`

PydanticAI **v2** agent: lease-aware typed tools that record steps, emit progress, and gate destructive actions with `ApprovalRequired`.

```python
"""The agent — PydanticAI v2.

Standard v2 Agent + tools (the v2 "capabilities" primitive is additive; the
Agent/@tool API here is unchanged). output_type includes DeferredToolRequests so
an approval-required tool ends the run with pending calls instead of executing.
Each tool waits for the lease, acts, records the step, and emits live progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.output import DeferredToolRequests

from .models import Action, ActionKind, Risk, StreamEvent
from .recorder import Recorder
from .registry import SessionRegistry


@dataclass
class AgentDeps:
    session_id: str
    chat_id: str
    lease_token: str
    registry: SessionRegistry
    recorder: Recorder
    emit: Callable[[StreamEvent], Awaitable[None]]
    _idx: list[int]                 # mutable step counter (one per chat turn run)

    def next_idx(self) -> int:
        self._idx[0] += 1
        return self._idx[0]


agent = Agent(
    "anthropic:claude-sonnet-4-6",
    deps_type=AgentDeps,
    output_type=[str, DeferredToolRequests],
    system_prompt=(
        "You drive a real web browser to accomplish the user's goal. Observe, "
        "then act one step at a time, referencing elements only by their ref "
        "ids. Call finish when done. Destructive actions (pay, delete, send, "
        "irreversible submits) require approval."
    ),
)


async def _run_action(ctx: RunContext[AgentDeps], action: Action) -> str:
    d = ctx.deps
    await d.registry.wait_until_agent_may_drive(d.session_id)   # block during takeover
    if not d.registry.holds(d.session_id, d.lease_token):
        return "Lease lost (human took over). Re-observe before continuing."
    session = d.registry.get(d.session_id)
    before = await session.observe()
    await d.emit(StreamEvent("action", d.chat_id,
                             {"action": action.kind.value, "ref": action.ref}))
    result = await session.dispatch(action, before)

    idx = d.next_idx()
    shot = await session.screenshot()
    await d.recorder.record(d.chat_id, d.session_id, idx, action, result, shot)

    await d.emit(StreamEvent("observation", d.chat_id,
                             {"idx": idx, "url": result.observation.url,
                              "ok": result.ok, "changed": result.changed}))
    obs = result.observation
    listing = "\n".join(f"{e.ref}: {e.role} '{e.name}'" for e in obs.elements[:60])
    status = "ok" if result.ok else f"error: {result.error}"
    moved = "changed" if result.changed else "NO CHANGE — may be stuck"
    return f"[{status}; {moved}] {obs.url}\n{listing}"


@agent.tool
async def navigate(ctx: RunContext[AgentDeps], url: str) -> str:
    """Go to a URL."""
    return await _run_action(ctx, Action(ActionKind.NAVIGATE, Risk.SAFE, url=url))


@agent.tool
async def act(ctx: RunContext[AgentDeps], ref: str, kind: str,
              text: str | None = None, submit: bool = False) -> str:
    """Interact with element `ref`. kind in click|type|select|scroll."""
    risk = _classify(kind, ref, text)
    if risk is Risk.DESTRUCTIVE and not ctx.tool_call_approved:
        raise ApprovalRequired
    a = Action(ActionKind(kind), risk, ref=ref, text=text, submit=submit)
    return await _run_action(ctx, a)


@agent.tool
async def extract(ctx: RunContext[AgentDeps], what: str) -> str:
    """Read data off the current page (no state change)."""
    return (await ctx.deps.registry.get(ctx.deps.session_id).observe()).text_digest


@agent.tool_plain
def finish(result: str) -> str:
    """Call when the goal is complete; `result` is the answer to the user."""
    return result


def _classify(kind: str, ref: str, text: str | None) -> Risk:
    blob = f"{ref} {text or ''}".lower()
    if any(w in blob for w in ("pay", "buy", "delete", "send", "confirm order",
                               "transfer", "checkout")):
        return Risk.DESTRUCTIVE
    if kind in ("type", "select") or "submit" in blob or "login" in blob:
        return Risk.SENSITIVE
    return Risk.SAFE
```

## `runner.py`

The v2 streaming loop — `run_stream_events` yields token/step events live, resolves approvals, resumes, and persists at turn end.

```python
"""Runner — one streamed agent turn (PydanticAI v2).

Uses agent.run_stream_events(): it runs the graph to completion while yielding
events. We translate them into WS events live — model tokens (TextPartDelta),
thinking, tool calls/results — and the final AgentRunResultEvent carries the run
result. If the output is DeferredToolRequests, we ask the user, then RESUME with
the same message history + a DeferredToolResults. Message history + storage_state
are persisted to Postgres at the end of the turn.
"""

from __future__ import annotations

import asyncio

from pydantic_ai import (
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)
from pydantic_ai.output import DeferredToolRequests
from pydantic_ai.tools import DeferredToolResults, ToolApproved, ToolDenied

from .agent import AgentDeps, agent
from .models import StreamEvent
from .recorder import Recorder
from .registry import SessionRegistry
from .store import Store


class Runner:
    def __init__(self, registry: SessionRegistry, store: Store,
                 recorder: Recorder) -> None:
        self.registry = registry
        self.store = store
        self.recorder = recorder
        self._pending: dict[str, asyncio.Future[dict]] = {}

    async def submit_approval(self, chat_id: str, decisions: dict) -> None:
        fut = self._pending.get(chat_id)
        if fut and not fut.done():
            fut.set_result(decisions)

    async def run_turn(self, session_id: str, chat_id: str, user_text: str,
                       emit) -> str:
        lease = await self.registry.acquire(session_id, "agent", chat_id)
        if lease is None:
            await emit(StreamEvent("error", chat_id,
                                   {"msg": "Session busy (human or another chat)."}))
            return ""
        deps = AgentDeps(session_id, chat_id, lease.token, self.registry,
                         self.recorder, emit, _idx=[0])
        history = await self.store.load_messages(chat_id)
        prompt: str | None = user_text
        deferred: DeferredToolResults | None = None
        try:
            while True:
                result = None
                async with agent.run_stream_events(
                    prompt, deps=deps, message_history=history,
                    deferred_tool_results=deferred,
                ) as events:
                    async for ev in events:
                        if isinstance(ev, AgentRunResultEvent):
                            result = ev.result
                        else:
                            await self._on_event(chat_id, ev, emit)

                prompt, deferred = None, None
                history = result.all_messages()

                if isinstance(result.output, DeferredToolRequests):
                    approvals = await self._collect(chat_id, result.output, emit)
                    deferred = DeferredToolResults(approvals=approvals)
                    continue

                # turn complete: persist and report
                await self.store.save_messages(chat_id, history)
                state = await self.registry.get(session_id).storage_state()
                await self.store.save_storage_state(session_id, state)
                await emit(StreamEvent("final", chat_id, {"text": result.output}))
                return result.output
        finally:
            await self.registry.release(session_id, lease.token)

    async def _on_event(self, chat_id: str, ev, emit) -> None:
        if isinstance(ev, PartDeltaEvent):
            if isinstance(ev.delta, TextPartDelta) and ev.delta.content_delta:
                await emit(StreamEvent("token", chat_id, {"text": ev.delta.content_delta}))
            elif isinstance(ev.delta, ThinkingPartDelta) and ev.delta.content_delta:
                await emit(StreamEvent("thinking", chat_id, {"text": ev.delta.content_delta}))
        elif isinstance(ev, FunctionToolCallEvent):
            await emit(StreamEvent("tool_call", chat_id,
                                   {"tool": ev.part.tool_name, "args": ev.part.args}))
        elif isinstance(ev, FunctionToolResultEvent):
            await emit(StreamEvent("tool_result", chat_id,
                                   {"tool_call_id": ev.tool_call_id}))

    async def _collect(self, chat_id: str, req: DeferredToolRequests, emit) -> dict:
        await emit(StreamEvent("approval_request", chat_id, {
            "calls": [{"id": c.tool_call_id, "tool": c.tool_name, "args": c.args}
                      for c in req.approvals]}))
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[chat_id] = fut
        decisions = await fut
        self._pending.pop(chat_id, None)

        out: dict[str, object] = {}
        for c in req.approvals:
            v = decisions.get(c.tool_call_id, False)
            out[c.tool_call_id] = ToolApproved() if v is True else ToolDenied(
                message=v if isinstance(v, str) else "Denied by user.")
        return out
```

## `evals.py`

`pydantic_evals` suite: cases run the agent end-to-end and score the outcome. Run in CI on every prompt/model/provider change.

```python
"""Eval suite (pydantic_evals).

Each Case is a goal + a checkable success criterion. The task function runs the
agent end-to-end against a throwaway session and returns the final state; custom
Evaluators score it. Run this in CI on every prompt/model/provider change — a
non-deterministic agent has no other ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from .agent import AgentDeps, agent
from .providers import make_provider
from .recorder import Recorder
from .registry import SessionRegistry
from .session import PlaywrightSession
from .store import Store


@dataclass
class Goal:
    instruction: str
    start_url: str


@dataclass
class Outcome:
    final_text: str
    final_url: str


class ReachedUrl(Evaluator[Goal, Outcome]):
    """Pass if the agent ended on a URL containing `needle`."""

    needle: str

    def evaluate(self, ctx: EvaluatorContext[Goal, Outcome]) -> float:
        return 1.0 if self.needle in ctx.output.final_url else 0.0


class Mentions(Evaluator[Goal, Outcome]):
    """Pass if the final answer mentions `needle` (cheap text check)."""

    needle: str

    def evaluate(self, ctx: EvaluatorContext[Goal, Outcome]) -> float:
        return 1.0 if self.needle.lower() in ctx.output.final_text.lower() else 0.0


async def _noop_emit(_):  # evals don't stream to a UI
    return None


def build_task(store: Store, recorder: Recorder, registry: SessionRegistry):
    async def task(goal: Goal) -> Outcome:
        sid = f"eval-{id(goal)}"
        await registry.create(sid, make_provider().name)  # fresh, isolated
        session: PlaywrightSession = registry.get(sid)
        await session._page.goto(goal.start_url)
        lease = await registry.acquire(sid, "agent", sid)
        deps = AgentDeps(sid, sid, lease.token, registry, recorder, _noop_emit, _idx=[0])
        result = await agent.run(goal.instruction, deps=deps)
        out = Outcome(final_text=str(result.output),
                      final_url=(await session.observe()).url)
        await registry.release(sid, lease.token)
        await session.close()
        return out

    return task


def suite() -> Dataset[Goal, Outcome]:
    return Dataset[Goal, Outcome](
        cases=[
            Case(
                name="hn_top_story",
                inputs=Goal("Open Hacker News and tell me the top story title.",
                            "https://news.ycombinator.com"),
                evaluators=[ReachedUrl(needle="ycombinator")],
            ),
            Case(
                name="wiki_search",
                inputs=Goal("Search Wikipedia for the Eiffel Tower and report its height.",
                            "https://en.wikipedia.org"),
                evaluators=[Mentions(needle="metres")],
            ),
        ]
    )


async def run_evals(store: Store, recorder: Recorder, registry: SessionRegistry):
    report = await suite().evaluate(build_task(store, recorder, registry))
    report.print()          # table of pass/fail, scores, durations
    return report
```

## `gateway.py`

FastAPI — REST plus the streaming chat WS (tokens + steps + approvals) and the view/takeover WS (screencast frames or iframe URL).

```python
"""Gateway — FastAPI. Hub for the React app.

  /ws/chat/{chat_id}    streams tokens + steps + approval requests; takes user
                        messages and approval decisions
  /ws/view/{session_id} on connect announces live-view mode. For 'screencast'
                        (local) it streams JPEG frames and accepts injected
                        input; for 'iframe' (browserbase) it sends the embed URL
                        and the frontend renders the iframe. Either way the lease
                        gates the agent on take_over/release.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .models import ProviderName, StreamEvent
from .recorder import LocalArtifacts, Recorder
from .registry import SessionRegistry
from .runner import Runner
from .store import Store

app = FastAPI()
state: dict = {}   # populated on startup: store, registry, recorder, runner


@app.on_event("startup")
async def _startup():
    store = await Store.connect(os.environ["DATABASE_URL"])
    registry = SessionRegistry(store)
    recorder = Recorder(store, LocalArtifacts())
    state.update(store=store, registry=registry, recorder=recorder,
                 runner=Runner(registry, store, recorder))

    async def reaper():
        while True:
            await asyncio.sleep(300)
            await registry.reap_idle()
    asyncio.create_task(reaper())


class CreateSession(BaseModel):
    session_id: str
    provider: ProviderName = "local"


class CreateChat(BaseModel):
    chat_id: str
    session_id: str


@app.post("/sessions")
async def create_session(body: CreateSession):
    await state["registry"].create(body.session_id, body.provider)
    return {"session_id": body.session_id, "provider": body.provider}


@app.post("/chats")
async def create_chat(body: CreateChat):
    state["registry"].attach(body.session_id, body.chat_id)
    await state["store"].bind_chat(body.chat_id, body.session_id)
    return {"chat_id": body.chat_id, "session_id": body.session_id}


# --- chat plane (streamed) ---------------------------------------------------
@app.websocket("/ws/chat/{chat_id}")
async def chat_ws(ws: WebSocket, chat_id: str):
    await ws.accept()
    store: Store = state["store"]
    session_id = await store.session_of(chat_id)
    if not session_id:
        await ws.close(code=4404)
        return

    async def emit(ev: StreamEvent) -> None:
        await ws.send_text(json.dumps({"type": ev.type, "data": ev.data}))

    runner: Runner = state["runner"]
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg["kind"] == "user_message":
                asyncio.create_task(
                    runner.run_turn(session_id, chat_id, msg["text"], emit))
            elif msg["kind"] == "approval":
                await runner.submit_approval(chat_id, msg["decisions"])
    except WebSocketDisconnect:
        state["registry"].detach(session_id, chat_id)


# --- live view + takeover plane ---------------------------------------------
@app.websocket("/ws/view/{session_id}")
async def view_ws(ws: WebSocket, session_id: str):
    await ws.accept()
    registry: SessionRegistry = state["registry"]
    session = registry.get(session_id)
    user_id = ws.query_params.get("user_id", "viewer")

    # announce how this session is viewed/controlled
    await ws.send_text(json.dumps({"type": "live_view",
                                   "mode": session.live_view_mode,
                                   "url": session.live_view_url}))

    async def send_frame(b64: str) -> None:
        await ws.send_text(json.dumps({"type": "frame", "data": b64}))

    if session.live_view_mode == "screencast":
        session.subscribe(send_frame)

    lease_token: str | None = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg["kind"]
            if kind == "take_over":
                lease = await registry.acquire(session_id, "human", user_id)
                lease_token = lease.token if lease else None
                await ws.send_text(json.dumps({"type": "lease",
                                               "granted": lease is not None}))
            elif kind == "release" and lease_token:
                await registry.release(session_id, lease_token)
                lease_token = None
            elif kind == "mouse" and lease_token:   # local only; iframe handles its own
                await session.inject_mouse(msg["x"], msg["y"],
                                           msg.get("event", "mousePressed"))
            elif kind == "key" and lease_token:
                await session.inject_key(msg["key"], msg.get("text"))
    except WebSocketDisconnect:
        if session.live_view_mode == "screencast":
            session.unsubscribe(send_frame)
        if lease_token:
            await registry.release(session_id, lease_token)
```

## How the pieces interact

### Streaming tokens + steps (v2)

`agent.run_stream_events(prompt, deps, message_history, deferred_tool_results)`
runs the graph to completion while yielding events. The runner maps them:

- `PartDeltaEvent` + `TextPartDelta.content_delta` -> `token` (word-by-word chat)
- `PartDeltaEvent` + `ThinkingPartDelta` -> `thinking`
- `FunctionToolCallEvent` / `FunctionToolResultEvent` -> `tool_call` / `tool_result`
- the tools themselves emit `action` / `observation` (with the recorded step idx)
- the final `AgentRunResultEvent` carries `result` (output + `all_messages()`)

So the UI sees the model *thinking and typing* and the browser *doing things*
at the same time. If `result.output` is `DeferredToolRequests`, the runner emits
`approval_request`, waits, then resumes the same run with `DeferredToolResults`.

> Alternative: PydanticAI v2 ships `VercelAIAdapter` / `AGUIAdapter` that encode
> this stream as SSE for those frontend protocols (AG-UI interrupts even map to
> deferred tools). Use an adapter if your React app speaks one of them; the
> custom WS here gives you full control over the message shapes instead.

### Swappable backend

`make_provider()` returns `LocalProvider` or `BrowserbaseProvider`. Local: we
hold a CDP session, so live view is `Page.startScreencast` frames and takeover
is `Input.dispatch*`. Browserbase: `sessions.create(keep_alive=True)` gives a
`connect_url` we drive over CDP, and `sessions.debug(id).debugger_fullscreen_url`
gives an embeddable iframe with built-in human takeover. The view WS announces
`{"type":"live_view","mode":"screencast"|"iframe","url":...}` on connect so the
frontend renders a `<canvas>` or an `<iframe>` accordingly. The lease still gates
the agent on `take_over`/`release` in *both* modes — otherwise the agent's CDP
calls would race a human driving the Browserbase iframe.

### Persistence

Message history is serialized with v2's `ModelMessagesTypeAdapter` and upserted
per chat, so a chat resumes with full context after a restart. `storage_state`
is saved per session at turn end and on idle-reap, and restored on `create`, so
logins survive. Every action writes a `steps` row + a screenshot artifact.

## React contract (WebSocket shapes)

Chat WS `/ws/chat/{chat_id}` — client sends:
```json
{"kind": "user_message", "text": "book the cheapest flight to NYC"}
{"kind": "approval", "decisions": {"call_abc": true, "call_xyz": "too expensive"}}
```
server streams: `token`, `thinking`, `tool_call`, `tool_result`, `action`,
`observation`, `approval_request`, `final`, `error`.

View WS `/ws/view/{session_id}?user_id=u1` — server first sends
`{"type":"live_view","mode":...,"url":...}`, then (screencast mode) a stream of
`{"type":"frame","data":"<base64 jpeg>"}`. Client sends:
```json
{"kind": "take_over"}
{"kind": "mouse", "x": 220, "y": 140, "event": "mousePressed"}   // screencast mode
{"kind": "key", "key": "Enter", "text": "\r"}                    // screencast mode
{"kind": "release"}
```
In iframe mode the user interacts with the embedded Browserbase iframe directly;
`take_over`/`release` still flip the lease so the agent pauses.

## Run

```bash
uvicorn app.gateway:app --reload
# POST /sessions {"session_id":"s1","provider":"local"}   (or "browserbase")
# POST /chats    {"chat_id":"c1","session_id":"s1"}
# open WS /ws/view/s1 to watch/take over, /ws/chat/c1 to drive

# evals (separate entrypoint):
python -c "import asyncio; from app.evals import run_evals; from app.store import Store; \
from app.registry import SessionRegistry; from app.recorder import Recorder, LocalArtifacts; \
asyncio.run((lambda: None)())"   # wire store/registry/recorder then: await run_evals(...)
```

## Notes / sharp edges

- Import paths reflect PydanticAI **v2.0**. If a `pydantic_ai.tools` /
  `pydantic_ai.output` symbol moves in a later 2.x, check the v2 API reference.
- `_classify` is keyword-based — a placeholder. Real destructive-action
  detection should inspect the resolved element/page, not substrings.
- `asyncpg` connection-per-op is fine for a skeleton; use the pool's
  `acquire()` context and consider statement caching under load.
- Browserbase live view embeds via `<iframe src=... sandbox="allow-same-origin
  allow-scripts">`; set `pointer-events` per whether you want passive viewing or
  takeover.