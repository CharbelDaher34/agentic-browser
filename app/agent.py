"""The agents — PydanticAI v2.

A *hybrid* browser agent that can act two complementary ways:

  • DOM mode    — list interactive elements (with `ref` ids) and act on them by
                  ref: navigate / act(ref, …) / extract. Cheap (text only).
  • Vision mode — `screenshot` to SEE the page, then act by pixel coordinates:
                  click_at / type_at / scroll / drag / press_key. `locate(desc)`
                  asks a vision model where a described element is. Handles
                  canvas/maps/icons and anything without a ref.

There are TWO agents sharing one browsing toolset:

  • `agent`    — the ORCHESTRATOR (talking model). Plans, talks to the user, and
                 can delegate side-quests to parallel sub-agents (`spawn_subagents`),
                 each driving its own tab. `output_type` includes
                 DeferredToolRequests so an approval-required tool pauses the run.
  • `subagent` — a focused worker (model chosen per task by the orchestrator) that
                 drives ONE tab and returns a CONCISE result, so the orchestrator's
                 context stays clean. Sub-agents cannot spawn further sub-agents.

Vision tools return the post-action screenshot via `ToolReturn` (multimodal) so
the model sees the result of each action.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    BinaryContent,
    DeferredToolRequests,
    RunContext,
    ToolReturn,
)
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import ApprovalRequired

from .config import settings
from .history import compact_history, strip_old_screenshots
from .models import Action, ActionKind, Risk, StreamEvent
from .models_registry import (
    ModelAlias,
    alias_choices,
    build_model,
    build_vision_model,
    resolve,
)
from .recorder import Recorder
from .registry import SessionRegistry

# Keep long conversations within the context window. strip first (cheap, removes
# old screenshots), then compact older turns if still large. Mutates only the
# per-request view — the persisted blob (and the UI trail) stays full-fidelity.
_HISTORY_CAPS = [ProcessHistory(strip_old_screenshots), ProcessHistory(compact_history)]


@dataclass
class AgentDeps:
    session_id: str
    chat_id: str
    lease_token: str
    registry: SessionRegistry
    recorder: Recorder
    emit: Callable[[StreamEvent], Awaitable[None]]
    _idx: list[int]                 # mutable step counter (shared across a chat turn)
    tab_id: str = "t0"              # which browser tab this agent drives
    depth: int = 0                  # 0 = orchestrator, >=1 = sub-agent
    label: str = "main"             # event label for the UI ("main" / "sub:t3")
    user_keys: dict = field(default_factory=dict)  # BYOK: {provider: api_key}

    def next_idx(self) -> int:
        self._idx[0] += 1
        return self._idx[0]


# ---- shared helpers --------------------------------------------------------
async def _run_action(
    ctx: RunContext[AgentDeps], action: Action, visual: bool = False
):
    """Perform an action on the agent's tab, record + stream it, return result.

    When `visual` is True the post-action screenshot is attached to the tool
    return (vision mode) so the model can act by coordinates next."""
    d = ctx.deps
    await d.registry.wait_until_agent_may_drive(d.session_id, d.tab_id)  # block during takeover
    if not d.registry.holds(d.session_id, d.lease_token, d.tab_id):
        return "Lease lost (human took over). Re-observe before continuing."
    session = d.registry.get(d.session_id)
    before = await session.observe(tab_id=d.tab_id)
    await d.emit(
        StreamEvent("action", d.chat_id, {
            "action": action.kind.value, "ref": action.ref,
            "agent": d.label, "tab": d.tab_id,
        })
    )
    result = await session.dispatch(action, before, tab_id=d.tab_id)

    idx = d.next_idx()
    shot = await session.screenshot(tab_id=d.tab_id)
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
                "agent": d.label,
                "tab": d.tab_id,
            },
        )
    )
    obs = result.observation
    listing = "\n".join(f"{e.ref}: {e.role} '{e.name}'" for e in obs.elements[:60])
    status = "ok" if result.ok else f"error: {result.error}"
    moved = "changed" if result.changed else "NO CHANGE — may be stuck"
    w, h = session.screen_size_of(d.tab_id)
    text_out = f"[{status}; {moved}] {obs.url} (screen {w}x{h}px)\n{listing}"
    if visual:
        return ToolReturn(
            return_value=text_out,
            content=[BinaryContent(data=shot, media_type="image/png")],
        )
    return text_out


def _gate(ctx: RunContext[AgentDeps], risk: Risk) -> str | None:
    """Approval gate. Returns a message string when the action is blocked (the
    caller should return it), or None to proceed.

    Orchestrator (depth 0): destructive + unapproved -> raise ApprovalRequired so
    the run pauses for the human gate. Sub-agents (depth>=1) cannot surface an
    approval dialog, so they get a message telling them to report back instead."""
    if risk is Risk.DESTRUCTIVE and not ctx.tool_call_approved:
        if ctx.deps.depth >= 1:
            return (
                "This action is destructive and sub-agents cannot request approval. "
                "Do NOT perform it — finish and report back to the main agent."
            )
        raise ApprovalRequired
    return None


# Whole-word matching (not substring) so benign text like "paypal", "resend", or
# "sending" doesn't trip the gate while "pay"/"send" as actual words still do.
_DESTRUCTIVE_RE = re.compile(
    r"\b(pay|buy|delete|send|confirm order|transfer|checkout)\b"
)
_SENSITIVE_RE = re.compile(r"\b(submit|login)\b")


def _classify(kind: str, ref: str, text: str | None) -> Risk:
    blob = f"{ref} {text or ''}".lower()
    if _DESTRUCTIVE_RE.search(blob):
        return Risk.DESTRUCTIVE
    if kind in ("type", "select") or _SENSITIVE_RE.search(blob):
        return Risk.SENSITIVE
    return Risk.SAFE


# ---- shared browsing tools (registered on BOTH agents) ---------------------
# DOM-ref tools
async def navigate(ctx: RunContext[AgentDeps], url: str) -> str:
    """Go to a URL."""
    return await _run_action(ctx, Action(ActionKind.NAVIGATE, Risk.SAFE, url=url))


async def act(
    ctx: RunContext[AgentDeps],
    ref: str,
    kind: str,
    text: str | None = None,
    submit: bool = False,
) -> str:
    """Interact with element `ref`. kind in click|type|select|scroll."""
    risk = _classify(kind, ref, text)
    blocked = _gate(ctx, risk)
    if blocked:
        return blocked
    a = Action(ActionKind(kind), risk, ref=ref, text=text, submit=submit)
    return await _run_action(ctx, a)


async def extract(ctx: RunContext[AgentDeps], what: str) -> str:
    """Read text off the current page (no state change)."""
    d = ctx.deps
    return (await d.registry.get(d.session_id).observe(tab_id=d.tab_id)).text_digest


# vision / coordinate tools
async def screenshot(ctx: RunContext[AgentDeps]) -> ToolReturn:
    """Capture the current page as an image so you can act by pixel coordinates."""
    d = ctx.deps
    await d.registry.wait_until_agent_may_drive(d.session_id, d.tab_id)
    session = d.registry.get(d.session_id)
    shot = await session.screenshot(tab_id=d.tab_id)
    w, h = session.screen_size_of(d.tab_id)
    return ToolReturn(
        return_value=(
            f"Screenshot of {session.url_of(d.tab_id)}. Screen is {w}x{h}px; coordinates "
            f"are pixels from the top-left. Use click_at/type_at/scroll with these."
        ),
        content=[BinaryContent(data=shot, media_type="image/png")],
    )


async def click_at(
    ctx: RunContext[AgentDeps], x: int, y: int, label: str = ""
) -> ToolReturn | str:
    """Click at pixel (x, y). Use for elements that have no ref (canvas, maps, icons).

    `label` = a short description of what you are clicking (e.g. 'Place order
    button'). It lets the approval gate catch destructive coordinate clicks just
    like the DOM `act` tool, so always set it when clicking buttons/links."""
    risk = _classify("click", label, None)
    blocked = _gate(ctx, risk)
    if blocked:
        return blocked
    return await _run_action(ctx, Action(ActionKind.CLICK_AT, risk, x=x, y=y), visual=True)


async def type_at(
    ctx: RunContext[AgentDeps],
    x: int,
    y: int,
    text: str,
    press_enter: bool = False,
    clear: bool = True,
) -> ToolReturn | str:
    """Click at pixel (x, y) then type `text`. Set press_enter to submit."""
    risk = _classify("type", "", text)
    blocked = _gate(ctx, risk)
    if blocked:
        return blocked
    return await _run_action(
        ctx,
        Action(ActionKind.TYPE_AT, risk, x=x, y=y, text=text, submit=press_enter, clear=clear),
        visual=True,
    )


async def scroll(
    ctx: RunContext[AgentDeps],
    direction: str = "down",
    x: int | None = None,
    y: int | None = None,
) -> ToolReturn:
    """Scroll the page up|down|left|right, optionally centered at pixel (x, y)."""
    d = ctx.deps
    w, h = d.registry.get(d.session_id).screen_size_of(d.tab_id)
    cx = x if x is not None else w // 2
    cy = y if y is not None else h // 2
    return await _run_action(
        ctx,
        Action(ActionKind.SCROLL_AT, Risk.SAFE, x=cx, y=cy, direction=direction, magnitude=600),
        visual=True,
    )


async def drag(
    ctx: RunContext[AgentDeps], x: int, y: int, to_x: int, to_y: int
) -> ToolReturn:
    """Drag from pixel (x, y) to (to_x, to_y)."""
    return await _run_action(
        ctx, Action(ActionKind.DRAG, Risk.SAFE, x=x, y=y, x2=to_x, y2=to_y), visual=True
    )


async def press_key(ctx: RunContext[AgentDeps], keys: str) -> ToolReturn | str:
    """Press a key or combination, e.g. 'Enter', 'Escape', 'Control+A'."""
    risk = _classify("key", keys, None)
    blocked = _gate(ctx, risk)
    if blocked:
        return blocked
    return await _run_action(ctx, Action(ActionKind.KEY, risk, keys=keys), visual=True)


async def go_back(ctx: RunContext[AgentDeps]) -> ToolReturn:
    """Navigate back to the previous page."""
    return await _run_action(ctx, Action(ActionKind.BACK, Risk.SAFE), visual=True)


async def go_forward(ctx: RunContext[AgentDeps]) -> ToolReturn:
    """Navigate forward to the next page."""
    return await _run_action(ctx, Action(ActionKind.FORWARD, Risk.SAFE), visual=True)


async def wait(ctx: RunContext[AgentDeps], seconds: int = 3) -> ToolReturn:
    """Wait for the page to settle (max 15s)."""
    return await _run_action(ctx, Action(ActionKind.WAIT, Risk.SAFE, seconds=seconds), visual=True)


# ---- locate (visual grounding via the Gemini robotics model) ---------------
class _Point(BaseModel):
    x: float = Field(description="horizontal pixel from the left edge")
    y: float = Field(description="vertical pixel from the top edge")
    found: bool = True


_locator = Agent(
    settings().agent_model,  # placeholder; every call overrides with build_vision_model()
    output_type=_Point,
    system_prompt=(
        "You are a precise UI visual-grounding model. Given a screenshot and a "
        "target description, return the PIXEL coordinates of the CENTER of the "
        "described element: x to the right from the left edge, y down from the top "
        "edge, both within the image dimensions stated in the prompt. If the target "
        "is not visible, set found=false."
    ),
)


async def locate(ctx: RunContext[AgentDeps], description: str) -> str:
    """Find where a described element is on screen. Returns its pixel (x, y) so you
    can then click_at/type_at there. Use when an element has no ref."""
    d = ctx.deps
    await d.registry.wait_until_agent_may_drive(d.session_id, d.tab_id)
    session = d.registry.get(d.session_id)
    shot = await session.screenshot(tab_id=d.tab_id)
    w, h = session.screen_size_of(d.tab_id)
    try:
        r = await _locator.run(
            [
                f"Find this element: {description}. The image is {w}x{h} pixels.",
                BinaryContent(data=shot, media_type="image/png"),
            ],
            model=build_vision_model(ctx.deps.user_keys),
            usage=ctx.usage,
        )
    except Exception as exc:  # noqa: BLE001
        return f"locate failed: {exc}"
    p = r.output
    if not p.found:
        return f"'{description}' is not visible on screen."
    x, y = float(p.x), float(p.y)
    # Some grounding models emit normalized 0–1000 coords; scale if they overshoot
    # the real viewport but fit in [0,1000].
    if (x > w or y > h) and x <= 1000 and y <= 1000:
        x, y = x / 1000 * w, y / 1000 * h
    x = max(0, min(int(round(x)), w - 1))
    y = max(0, min(int(round(y)), h - 1))
    return f"Found '{description}' at ({x}, {y}). Use click_at/type_at with these coordinates."


def finish(result: str) -> str:
    """Call when the goal is complete; `result` is the answer to the user."""
    return result


# browsing tools shared by both agents (the orchestrator also gets `finish`)
_BROWSING_TOOLS = [
    navigate, act, extract, screenshot, click_at, type_at, scroll, drag,
    press_key, go_back, go_forward, wait, locate,
]
# Sub-agents have output_type=str — they finish simply by replying with their
# report, so they must NOT have `finish` (calling it and then stopping leaves the
# run with no string output → "exceeded output retries"). The orchestrator keeps it.
_ORCHESTRATOR_TOOLS = _BROWSING_TOOLS + [finish]

_BROWSE_HELP = (
    "You drive a real web browser.\n"
    "Act two complementary ways — pick whichever fits each step:\n"
    "1) DOM mode: `navigate(url)`, `act(ref, kind=click|type|select|scroll, ...)`, `extract`. "
    "Action results list interactive elements as 'ref: role \"name\"'. Prefer DOM mode when the "
    "target has a ref — precise and cheap.\n"
    "2) Vision mode: `screenshot` to SEE the page, then act by PIXEL coordinates with "
    "`click_at(x, y, label)`, `type_at(x, y, text)`, `scroll(direction)`, `drag(...)`. Use "
    "`locate(description)` to get the pixel coordinates of a described element (a vision model "
    "finds it), then click_at/type_at there. Set `click_at`'s `label` to what it does "
    "(e.g. 'Pay now') so destructive clicks still hit the approval gate.\n"
    "Also: `go_back`, `go_forward`, `press_key(keys)`, `wait(seconds)`. Act ONE step at a time "
    "and verify before the next."
)


SUBAGENT_PROMPT = (
    "You are a focused worker driving ONE browser tab to complete a single assigned task.\n\n"
    + _BROWSE_HELP
    + "\n\nYou CANNOT spawn further sub-agents. Avoid destructive actions (pay/delete/send); if "
    "the task requires one, stop and report that back. When the task is complete, simply REPLY "
    "with a CONCISE report (key facts and outcome only, a few lines) — that reply is returned to "
    "the main agent, which only sees this summary, not your steps. Do not narrate every step."
)

subagent = Agent(
    settings().agent_model,
    deps_type=AgentDeps,
    output_type=str,
    tools=_BROWSING_TOOLS,
    system_prompt=SUBAGENT_PROMPT,
    capabilities=_HISTORY_CAPS,
    retries=3,
)


ORCHESTRATOR_PROMPT = (
    "You are the orchestrator: you understand the user's goal, plan, talk to the user, and drive "
    "the browser.\n\n"
    + _BROWSE_HELP
    + "\n\nDelegation: for independent sub-tasks or side-quests, call `spawn_subagents(tasks=[…])` — "
    f"each task runs on its OWN browser tab in parallel (up to {settings().max_concurrent_subagents} "
    "at once) and returns a CONCISE result, keeping your context focused. For each task pick a "
    f"`model_alias` from {alias_choices()}: 'fast' for simple lookups, 'smart' for general work, "
    "'deep' for hard reasoning. Optionally pass an existing `tab` id to reuse a tab; otherwise a new "
    "one is opened. Manage tabs with `open_tab`/`list_tabs`/`close_tab`.\n\n"
    "Call `finish(result)` when the goal is complete. Destructive actions (pay, delete, send, "
    "irreversible submits) require user approval."
)

agent = Agent(
    settings().agent_model,
    deps_type=AgentDeps,
    output_type=[str, DeferredToolRequests],
    tools=_ORCHESTRATOR_TOOLS,
    system_prompt=ORCHESTRATOR_PROMPT,
    capabilities=_HISTORY_CAPS,
    retries=3,
)


# ---- orchestrator-only tools (delegation + tab management) -----------------
class SubTask(BaseModel):
    task: str = Field(description="the self-contained instruction for the sub-agent")
    model_alias: ModelAlias = Field(
        default=ModelAlias.smart, description="which model the sub-agent should run on"
    )
    tab: str | None = Field(
        default=None, description="existing tab id to reuse, or null to open a new tab"
    )


async def _run_subtask(ctx: RunContext[AgentDeps], t: SubTask) -> dict:
    d = ctx.deps
    session = d.registry.get(d.session_id)
    tab_id = t.tab if (t.tab and session.has_tab(t.tab)) else await session.open_tab(label=t.task[:40])
    holder = f"{d.chat_id}:{tab_id}"
    lease = await d.registry.acquire(d.session_id, "agent", holder, tab_id=tab_id)
    if lease is None:
        return {"tab": tab_id, "result": "tab busy (human or another agent is driving it)"}
    # Everything after acquire() lives in the try so the finally always releases —
    # even if the task is cancelled (user steers mid-run) during the start emit.
    try:
        sub_deps = AgentDeps(
            session_id=d.session_id, chat_id=d.chat_id, lease_token=lease.token,
            registry=d.registry, recorder=d.recorder, emit=d.emit, _idx=d._idx,
            tab_id=tab_id, depth=d.depth + 1, label=f"sub:{tab_id}", user_keys=d.user_keys,
        )
        await d.emit(StreamEvent("subagent_start", d.chat_id, {
            "id": tab_id, "task": t.task, "model": t.model_alias.value, "tab": tab_id,
        }))
        r = await subagent.run(
            t.task, deps=sub_deps,
            model=build_model(resolve(t.model_alias), ctx.deps.user_keys),
            usage=ctx.usage,
        )
        await d.emit(StreamEvent("subagent_end", d.chat_id, {
            "id": tab_id, "result": r.output, "ok": True,
        }))
        return {"tab": tab_id, "result": r.output}
    except Exception as exc:  # noqa: BLE001
        await d.emit(StreamEvent("subagent_end", d.chat_id, {
            "id": tab_id, "result": str(exc), "ok": False,
        }))
        raise
    finally:
        await d.registry.release(d.session_id, lease.token, tab_id=tab_id)


@agent.tool
async def spawn_subagents(ctx: RunContext[AgentDeps], tasks: list[SubTask]) -> str:
    """Delegate independent sub-tasks to parallel sub-agents, each on its own tab.

    Returns a concise digest of every sub-agent's result."""
    d = ctx.deps
    cfg = settings()
    if d.depth >= cfg.max_subagent_depth:
        return "Sub-agents cannot spawn their own sub-agents — do the work directly."
    if not tasks:
        return "No tasks provided."
    if len(tasks) > cfg.max_concurrent_subagents:
        return (
            f"Too many tasks ({len(tasks)}); spawn at most "
            f"{cfg.max_concurrent_subagents} at once."
        )
    session = d.registry.get(d.session_id)
    # Two sub-agents can't drive the same tab — reject duplicate `tab` targets
    # (otherwise they'd race on one page and clobber each other's lease).
    reused = [t.tab for t in tasks if t.tab and session.has_tab(t.tab)]
    if len(reused) != len(set(reused)):
        return ("Two sub-tasks target the same tab; give each its own `tab` id or "
                "omit `tab` to open a fresh one.")
    open_now = len(session.list_tabs())
    new_needed = sum(1 for t in tasks if not (t.tab and session.has_tab(t.tab)))
    if open_now + new_needed > cfg.max_tabs:
        return (
            f"Tab budget exceeded (open {open_now}, max {cfg.max_tabs}); reuse tabs "
            "or spawn fewer sub-agents."
        )
    results = await asyncio.gather(
        *[_run_subtask(ctx, t) for t in tasks], return_exceptions=True
    )
    lines = []
    for t, res in zip(tasks, results):
        if isinstance(res, Exception):
            lines.append(f"- [{t.task[:50]}] ERROR: {res}")
        else:
            lines.append(f"- [{res['tab']}] {t.task[:50]}: {res['result']}")
    return "Sub-agent results:\n" + "\n".join(lines)


@agent.tool
async def spawn_subagent(
    ctx: RunContext[AgentDeps],
    task: str,
    model_alias: ModelAlias = ModelAlias.smart,
    tab: str | None = None,
) -> str:
    """Delegate a single side-task to one sub-agent on its own tab; returns its result."""
    return await spawn_subagents(
        ctx, [SubTask(task=task, model_alias=model_alias, tab=tab)]
    )


@agent.tool
async def open_tab(ctx: RunContext[AgentDeps], url: str | None = None, label: str = "") -> str:
    """Open a new browser tab (optionally navigating to `url`). Returns its tab id."""
    d = ctx.deps
    session = d.registry.get(d.session_id)
    if len(session.list_tabs()) >= settings().max_tabs:
        return f"Tab budget reached (max {settings().max_tabs}); close a tab first."
    tab_id = await session.open_tab(url=url, label=label)
    return f"Opened tab {tab_id}" + (f" at {url}" if url else "") + "."


@agent.tool
async def list_tabs(ctx: RunContext[AgentDeps]) -> str:
    """List the open browser tabs (id, label, url)."""
    tabs = ctx.deps.registry.get(ctx.deps.session_id).list_tabs()
    return "Open tabs:\n" + "\n".join(
        f"- {t['tab_id']}{' (primary)' if t['primary'] else ''}: "
        f"{t['label'] or '—'} {t['url']}"
        for t in tabs
    )


@agent.tool
async def close_tab(ctx: RunContext[AgentDeps], tab_id: str) -> str:
    """Close a non-primary browser tab."""
    await ctx.deps.registry.get(ctx.deps.session_id).close_tab(tab_id)
    return f"Closed tab {tab_id}."
