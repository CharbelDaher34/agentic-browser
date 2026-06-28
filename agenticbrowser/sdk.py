# SPDX-License-Identifier: Apache-2.0
"""BrowserAgent — the embeddable async SDK.

The headless way to drive the agent from your own process:

    async with BrowserAgent(keys={"anthropic": "sk-..."}) as agent:
        result = await agent.run("find the cheapest direct LON->NYC next Friday")
        print(result.output)

It is a thin wrapper over the existing `Runner`: it mints an internal
session/chat id, builds a `MemoryStore` (or `SqliteStore` when `persist=` is set)
+ a `Recorder` + a single-session `SessionRegistry`, and drives `Runner.run_turn`.
Streaming, the approval pause/resume, history persistence, and budgets all come
from the core unchanged.

Approval gate: destructive actions (pay/buy/delete/send/checkout) pause the run.
Provide `approve=` to decide; OMIT it and they are auto-DENIED (fail-safe).

Deferred to a later milestone: `live_view()` / `takeover()` / `send_input()`
(interactive human-takeover) and mid-run `steer()`.
Multi-turn conversation already works by calling `run()`/`stream()` again on the
same instance — history accumulates in the store under one chat id.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from .artifacts import ArtifactStore, MemoryArtifacts
from .config import CoreConfig
from .events import StreamEvent
from .recorder import Recorder
from .registry import SessionRegistry
from .runner import Runner
from .stores import MemoryStore, SqliteStore, Store


@dataclass
class ApprovalRequest:
    """A destructive action the agent wants to take, surfaced for a decision."""

    tool: str
    args: Any
    id: str


@dataclass
class Approval:
    approved: bool
    reason: str | None = None

    @classmethod
    def allow(cls) -> "Approval":
        return cls(True)

    @classmethod
    def deny(cls, reason: str = "Denied.") -> "Approval":
        return cls(False, reason)


# An approve handler may return an Approval, or a bool (True=allow), or a str
# (deny with that reason). It may be sync or async.
ApproveFn = Callable[[ApprovalRequest], "Approval | bool | str | Awaitable[Any]"]


@dataclass
class RunResult:
    output: str                      # the agent's final answer
    steps: list[dict] = field(default_factory=list)      # the replay trail
    usage: dict = field(default_factory=dict)            # tokens/requests/steps/cost
    approvals: list[dict] = field(default_factory=list)  # approval requests seen


_SENTINEL = object()


def _normalize(v: Any) -> Approval:
    if isinstance(v, Approval):
        return v
    if v is True:
        return Approval.allow()
    if isinstance(v, str):
        return Approval.deny(v)
    return Approval.deny()


class BrowserAgent:
    def __init__(
        self,
        *,
        keys: dict[str, str] | None = None,
        backend: str = "local",
        browserbase: dict | None = None,
        headless: bool = True,
        model: str | None = None,
        worker_model: str | None = None,
        approve: ApproveFn | None = None,
        subagents: bool = False,
        max_concurrent_subagents: int = 1,
        max_tabs: int = 6,
        persist: str | Store | None = None,
        artifacts: ArtifactStore | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> None:
        self._keys = dict(keys or {})
        self._approve = approve
        self._persist = persist
        self._artifacts_override = artifacts
        self._browserbase = browserbase
        self._cfg = CoreConfig(
            browser_provider=backend,
            headless=headless,
            agent_model=model or CoreConfig().agent_model,
            worker_model=worker_model,
            # the integrator's `keys=` ARE the keys this process uses — nothing is
            # read from os.environ, and there are no per-session keys.
            provider_keys=dict(self._keys),
            max_subagent_depth=1 if subagents else 0,
            max_concurrent_subagents=max_concurrent_subagents,
            max_tabs=max_tabs,
            browserbase_api_key=(browserbase or {}).get("api_key"),
            browserbase_project_id=(browserbase or {}).get("project_id"),
            max_steps=max_steps,
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
        )
        self._sid = f"sdk-{uuid.uuid4().hex[:12]}"
        self._cid = f"chat-{uuid.uuid4().hex[:12]}"
        self._store: Store | None = None
        self._registry: SessionRegistry | None = None
        self._runner: Runner | None = None
        self._opened = False
        self._lock = asyncio.Lock()  # one turn at a time per agent (serialize run/stream)

    @staticmethod
    def keys_from_env() -> dict[str, str]:
        """Collect provider keys from the usual env vars (ANTHROPIC/OPENAI/GEMINI)."""
        import os

        from .models_registry import KEY_ENV_VARS

        return {p: os.environ[v] for p, v in KEY_ENV_VARS.items() if os.environ.get(v)}

    def _make_store(self) -> Store:
        # keys + browserbase creds live in CoreConfig, not the store.
        if self._persist is None:
            return MemoryStore()
        # a custom Store instance (the runtime-checkable Protocol) is used as-is
        if isinstance(self._persist, Store):
            return self._persist  # type: ignore[return-value]
        path = self._persist
        if isinstance(path, str) and path.startswith("sqlite:///"):
            path = path[len("sqlite:///"):]
        return SqliteStore(str(path))

    # ---- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "BrowserAgent":
        self._store = self._make_store()
        artifacts: ArtifactStore = self._artifacts_override or MemoryArtifacts()
        self._registry = SessionRegistry(self._store, self._cfg)
        self._recorder = Recorder(self._store, artifacts)
        self._runner = Runner(self._registry, self._store, self._recorder, self._cfg)
        try:
            await self._registry.create(self._sid, self._cfg.browser_provider)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Failed to open a browser ({exc}). For the local backend, install "
                "Chromium once with `python -m agenticbrowser.install` (or "
                "`playwright install chromium`), or use backend='browserbase'."
            ) from exc
        self._opened = True
        return self

    async def __aexit__(self, *exc) -> None:
        if self._registry is not None:
            try:
                await self._registry.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if isinstance(self._store, SqliteStore):
            try:
                await self._store.close()
            except Exception:  # noqa: BLE001
                pass
        self._opened = False

    # ---- approval bridge ---------------------------------------------------
    async def _resolve_approval(self, chat_id: str, data: dict, approve: ApproveFn | None) -> None:
        # MUST resolve every pending call: Runner._collect awaits a future with NO
        # timeout, so a raising handler (or a malformed call) would otherwise hang
        # the run forever. Any error -> deny that call (fail-safe), and always submit.
        decisions: dict[str, Any] = {}
        for call in data.get("calls", []):
            cid = call.get("id")
            try:
                if approve is None:
                    verdict = Approval.deny(
                        "auto-denied (no approve= handler; destructive action blocked)"
                    )
                else:
                    r = approve(ApprovalRequest(tool=call.get("tool", ""), args=call.get("args"), id=cid))
                    if inspect.isawaitable(r):
                        r = await r
                    verdict = _normalize(r)
            except Exception as exc:  # noqa: BLE001 — never let approval hang the run
                verdict = Approval.deny(f"approval handler error: {exc}")
            if cid is not None:
                decisions[cid] = True if verdict.approved else (verdict.reason or "Denied.")
        try:
            await self._runner.submit_approval(chat_id, decisions)
        except Exception:  # noqa: BLE001
            pass

    def _spawn(self, goal: str, approve: ApproveFn | None):
        """Launch the turn; return (queue, task). Events flow onto the queue; the
        task resolves to the final output (and re-raises any error)."""
        if not self._opened:
            raise RuntimeError("Open the agent first: `async with BrowserAgent(...) as agent:`.")
        q: asyncio.Queue = asyncio.Queue()

        async def emit(ev: StreamEvent) -> None:
            await q.put(ev)
            if ev.type == "approval_request":
                # resolve in a separate task: Runner._collect registers the pending
                # future right AFTER emitting, so we must not call submit_approval
                # inline (the future wouldn't exist yet).
                asyncio.create_task(self._resolve_approval(ev.chat_id, dict(ev.data), approve))

        async def wrapper() -> str:
            try:
                return await self._runner.run_turn(self._sid, self._cid, goal, emit)
            finally:
                await q.put(_SENTINEL)

        return q, asyncio.create_task(wrapper())

    # ---- run / stream ------------------------------------------------------
    # The per-agent lock serializes turns: concurrent run()/stream() calls (e.g. a
    # host issuing parallel tool calls on one EphemeralBrowser) QUEUE rather than the
    # 2nd being rejected by the runner's in-flight guard and returning "".
    async def stream(self, goal: str, approve: ApproveFn | None = None) -> AsyncIterator[StreamEvent]:
        """Run one turn, yielding StreamEvents live. `approve` overrides the
        constructor handler for this call (auto-deny if neither is set)."""
        if not self._opened:
            raise RuntimeError("Open the agent first: `async with BrowserAgent(...) as agent:`.")
        async with self._lock:
            q, task = self._spawn(goal, approve if approve is not None else self._approve)
            try:
                while True:
                    ev = await q.get()
                    if ev is _SENTINEL:
                        break
                    yield ev
            finally:
                await task  # surface the final output / re-raise any error

    async def run(self, goal: str, approve: ApproveFn | None = None) -> RunResult:
        """Run one turn to completion and return a structured result."""
        if not self._opened:
            raise RuntimeError("Open the agent first: `async with BrowserAgent(...) as agent:`.")
        async with self._lock:
            start_idx = await self._store.max_step_idx(self._cid)
            q, task = self._spawn(goal, approve if approve is not None else self._approve)
            events: list[StreamEvent] = []
            while True:
                ev = await q.get()
                if ev is _SENTINEL:
                    break
                events.append(ev)
            output = (await task) or ""
            # only THIS turn's steps (since=start_idx) — avoids re-reading the whole
            # trail every turn on a long-lived agent.
            steps = (
                await self._store.list_steps(self._cid, since=start_idx)
                if hasattr(self._store, "list_steps")
                else []
            )
            usage = next((dict(e.data) for e in reversed(events) if e.type == "usage"), {})
            approvals = [dict(e.data) for e in events if e.type == "approval_request"]
            return RunResult(output=output, steps=steps, usage=usage, approvals=approvals)

    # ---- conversation state ------------------------------------------------
    async def export_messages(self) -> list:
        """The persisted PydanticAI message history for this agent's chat."""
        return await self._store.load_messages(self._cid)

    @property
    def session_id(self) -> str:
        return self._sid

    @property
    def chat_id(self) -> str:
        return self._cid
