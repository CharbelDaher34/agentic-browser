"""The agent — PydanticAI v2.

Standard v2 Agent + tools. output_type includes DeferredToolRequests so an
approval-required tool ends the run with pending calls instead of executing.
Each tool waits for the lease, acts, records the step, and emits live progress.

NOTE vs the design spec: `DeferredToolRequests` is imported from `pydantic_ai`
(top level) — in v2.0.0 it is *not* under `pydantic_ai.output`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from .config import settings
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
    settings().agent_model,
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
    await d.emit(
        StreamEvent("action", d.chat_id, {"action": action.kind.value, "ref": action.ref})
    )
    result = await session.dispatch(action, before)

    idx = d.next_idx()
    shot = await session.screenshot()
    await d.recorder.record(d.chat_id, d.session_id, idx, action, result, shot)

    await d.emit(
        StreamEvent(
            "observation",
            d.chat_id,
            {
                "idx": idx,
                "url": result.observation.url,
                "title": result.observation.title,
                "ok": result.ok,
                "changed": result.changed,
            },
        )
    )
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
async def act(
    ctx: RunContext[AgentDeps],
    ref: str,
    kind: str,
    text: str | None = None,
    submit: bool = False,
) -> str:
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
    if any(
        w in blob
        for w in (
            "pay", "buy", "delete", "send", "confirm order", "transfer", "checkout",
        )
    ):
        return Risk.DESTRUCTIVE
    if kind in ("type", "select") or "submit" in blob or "login" in blob:
        return Risk.SENSITIVE
    return Risk.SAFE
