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
import logging
from dataclasses import dataclass, field

from pydantic_ai import (
    AgentRunResultEvent,
    DeferredToolRequests,
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ThinkingPartDelta,
)
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.tools import DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.usage import UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from .agent import AgentDeps, agent
from .config import CoreConfig
from .history import well_formed
from .models import StreamEvent
from .models_registry import resolve_model
from .recorder import Recorder
from .registry import SessionRegistry
from .stores import Store

log = logging.getLogger(__name__)


@dataclass
class _Accum:
    """Accumulates an in-flight turn's streamed text/thinking so a partial can be
    persisted if the user interrupts mid-run."""
    text: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)

    def text_blob(self) -> str:
        return "".join(self.text)

    def think_blob(self) -> str:
        return "".join(self.thinking)


class Runner:
    def __init__(
        self,
        registry: SessionRegistry,
        store: Store,
        recorder: Recorder,
        cfg: CoreConfig | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self._cfg = cfg or CoreConfig()
        self.recorder = recorder
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._inflight: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        self._runs: dict[str, asyncio.Task] = {}        # chat_id -> active run task
        self._partials: dict[str, _Accum] = {}          # chat_id -> streamed accumulator

    async def submit_approval(self, chat_id: str, decisions: dict) -> None:
        fut = self._pending.get(chat_id)
        if fut and not fut.done():
            fut.set_result(decisions)

    def cancel_pending(self, chat_id: str) -> None:
        """Cancel an approval the runner is blocked on (e.g. the chat WS dropped),
        so run_turn unwinds, releases the lease, and clears in-flight state."""
        fut = self._pending.get(chat_id)
        if fut and not fut.done():
            fut.cancel()

    async def start_turn(
        self, session_id: str, chat_id: str, user_text: str, emit, user_id: str = ""
    ) -> None:
        """Begin a turn, INTERRUPTING any turn already running for this chat.

        Mid-conversation steering: a new message stops all in-flight work for the
        chat first (cancelling the run task — which cascades to sub-agent tasks and
        releases every lease — and persisting the partial context), then launches
        the new turn."""
        await self.stop(chat_id)
        task = asyncio.create_task(
            self.run_turn(session_id, chat_id, user_text, emit, user_id)
        )
        self._runs[chat_id] = task

        def _done(t: asyncio.Task, c: str = chat_id) -> None:
            if self._runs.get(c) is t:
                self._runs.pop(c, None)

        task.add_done_callback(_done)

    async def stop(self, chat_id: str) -> None:
        """Cancel the in-flight turn for a chat and AWAIT its unwind, so the lease
        is released and partial context persisted before anything new starts."""
        task = self._runs.get(chat_id)
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def run_turn(
        self, session_id: str, chat_id: str, user_text: str, emit, user_id: str = ""
    ) -> str:
        # One agent turn per chat at a time. Without this, a second concurrent
        # message on the same chat would share the (re-entrant) lease and the two
        # turns would clobber each other's message history and step indices.
        async with self._inflight_lock:
            if chat_id in self._inflight:
                await emit(StreamEvent(
                    "error", chat_id,
                    {"msg": "A turn is already running for this chat — wait for it to finish."},
                ))
                return ""
            self._inflight.add(chat_id)

        lease = None
        history: list = []
        prompt: str | None = user_text
        acc = self._partials[chat_id] = _Accum()
        try:
            # Make sure the browser session is live (rehydrate after a restart).
            if not await self.registry.ensure(session_id):
                await emit(StreamEvent("error", chat_id, {"msg": "Unknown browser session."}))
                return ""
            self.registry.attach(session_id, chat_id)

            lease = await self.registry.acquire(session_id, "agent", chat_id)
            if lease is None:
                await emit(StreamEvent(
                    "error", chat_id, {"msg": "Session busy (human or another chat)."}
                ))
                return ""

            # continue the per-chat step counter rather than resetting to 0,
            # so step idx stays monotonic across turns (correct replay trail).
            start_idx = await self.store.max_step_idx(chat_id)
            deps = AgentDeps(
                session_id, chat_id, lease.token, self.registry, self.recorder,
                emit, _idx=[start_idx], cfg=self._cfg,
            )
            # orchestrator runs on the configured agent_model (resolved to whichever
            # provider this process has a key for)
            orch_model = resolve_model(self._cfg.agent_model, self._cfg)
            # Heal any history saved with a dangling tool_use (e.g. a turn that was
            # interrupted while a tool call / approval was pending) so Anthropic
            # doesn't reject it; also re-persists the cleaned history at turn end.
            history = well_formed(await self.store.load_messages(chat_id))
            deferred: DeferredToolResults | None = None

            # cost observability (W-C): accumulate token/request usage across the
            # turn's segments (a turn can span multiple runs when approvals pause it)
            # and enforce the configured budgets. request_limit is disabled (None) so
            # we don't inherit PydanticAI's default 50-request cap — a browsing turn
            # legitimately makes many requests; max_steps/max_tokens are the knobs.
            usage_in = usage_out = usage_req = 0

            def _usage_payload() -> dict:
                # the turn's accumulated usage so far — emitted on EVERY terminal
                # path (final, interrupted, budget) so the UI always gets a total.
                return {
                    "steps": max(0, deps._idx[0] - start_idx),
                    "requests": usage_req,
                    "input_tokens": usage_in,
                    "output_tokens": usage_out,
                    "total_tokens": usage_in + usage_out,
                    "cost_usd": None,  # populated when the W-C price table lands
                }

            ulimits = None
            if self._cfg.max_steps or self._cfg.max_tokens:
                ulimits = UsageLimits(
                    request_limit=None,
                    tool_calls_limit=self._cfg.max_steps,
                    total_tokens_limit=self._cfg.max_tokens,
                )

            while True:
                result = None
                async with agent.run_stream_events(
                    prompt,
                    deps=deps,
                    message_history=history,
                    deferred_tool_results=deferred,
                    model=orch_model,
                    usage_limits=ulimits,
                ) as events:
                    async for ev in events:
                        if isinstance(ev, AgentRunResultEvent):
                            result = ev.result
                        else:
                            await self._on_event(chat_id, ev, emit)

                prompt, deferred = None, None
                history = result.all_messages()
                try:
                    # pydantic-ai v2: `result.usage` is a property (RunUsage), not a
                    # method — calling it raises and (formerly) silently zeroed usage.
                    _u = result.usage
                    usage_in += _u.input_tokens
                    usage_out += _u.output_tokens
                    usage_req += _u.requests
                except Exception:  # noqa: BLE001 — never fail a turn on usage accounting
                    # log (don't crash) so a future pydantic-ai API change that breaks
                    # usage reading is visible instead of silently showing 0 tokens.
                    log.warning("usage accounting failed for chat %s", chat_id, exc_info=True)

                if isinstance(result.output, DeferredToolRequests):
                    approvals = await self._collect(chat_id, result.output, emit)
                    deferred = DeferredToolResults(approvals=approvals)
                    continue

                # turn complete: persist and report
                await self.store.save_messages(chat_id, history)
                try:
                    sess = self.registry.get(session_id)
                    state = await sess.storage_state()
                    await self.store.save_storage_state(session_id, state, last_url=sess.url)
                except Exception:  # noqa: BLE001
                    pass
                await emit(StreamEvent("usage", chat_id, _usage_payload()))
                await emit(StreamEvent("final", chat_id, {"text": result.output}))
                return result.output
        except asyncio.CancelledError:
            # Interrupted (user steered mid-run, or the chat WS dropped). Keep
            # partial context: persist what the assistant produced so far + an
            # interruption marker so the next turn builds on it. Shield the save so
            # the cancellation that's unwinding us doesn't abort the write.
            try:
                await asyncio.shield(self._persist_partial(chat_id, history, prompt, acc))
            except Exception:  # noqa: BLE001
                pass
            try:
                await emit(StreamEvent("usage", chat_id, _usage_payload()))
            except Exception:  # noqa: BLE001 — never block the unwind on usage
                pass
            await emit(StreamEvent("interrupted", chat_id, {"text": acc.text_blob()}))
            raise
        except UsageLimitExceeded as exc:
            # a configured budget (max_steps/max_tokens) was hit — report cleanly.
            try:
                await emit(StreamEvent("usage", chat_id, _usage_payload()))
            except Exception:  # noqa: BLE001
                pass
            await emit(StreamEvent("error", chat_id, {"msg": f"Budget exceeded: {exc}"}))
            return ""
        except Exception as exc:  # noqa: BLE001
            await emit(StreamEvent("error", chat_id, {"msg": f"Agent error: {exc}"}))
            return ""
        finally:
            self._partials.pop(chat_id, None)
            if lease is not None:
                await self.registry.release(session_id, lease.token)
            # close sub-agent tabs opened this turn so they don't leak across turns,
            # and cancel any detached popup tasks so they can't mutate the page after.
            try:
                session = self.registry.get(session_id)
                await session.cancel_background()
                for t in session.list_tabs():
                    if not t["primary"]:
                        await session.close_tab(t["tab_id"])
            except Exception:  # noqa: BLE001
                pass
            self._inflight.discard(chat_id)

    async def _persist_partial(
        self, chat_id: str, history: list, prompt: str | None, acc: _Accum
    ) -> None:
        """On interrupt, append the user's (uncommitted) message + the partial
        assistant output + an interruption marker, then save. Text/thinking only —
        no dangling tool calls — so the stored history stays well-formed."""
        # drop any trailing dangling tool_use (approval-pending / mid-tool) so we
        # never persist a history Anthropic would reject next turn.
        msgs = list(well_formed(history))
        if prompt:  # this turn's user message hasn't been folded into history yet
            msgs.append(ModelRequest(parts=[UserPromptPart(content=prompt)]))
        parts: list = []
        think = acc.think_blob()
        if think:
            parts.append(ThinkingPart(content=think))
        body = (acc.text_blob() + "\n\n[interrupted by user]").strip()
        parts.append(TextPart(content=body))
        msgs.append(ModelResponse(parts=parts))
        await self.store.save_messages(chat_id, msgs)

    async def _on_event(self, chat_id: str, ev, emit) -> None:
        acc = self._partials.get(chat_id)
        # A new text/thinking part can carry initial content on PartStartEvent (not
        # just deltas); without this the first chunk of the answer is dropped.
        if isinstance(ev, PartStartEvent):
            part = ev.part
            content = getattr(part, "content", None)
            pk = getattr(part, "part_kind", "")
            if isinstance(content, str) and content:
                if pk == "text":
                    if acc is not None:
                        acc.text.append(content)
                    await emit(StreamEvent("token", chat_id, {"text": content}))
                elif pk == "thinking":
                    if acc is not None:
                        acc.thinking.append(content)
                    await emit(StreamEvent("thinking", chat_id, {"text": content}))
            return
        if isinstance(ev, PartDeltaEvent):
            if isinstance(ev.delta, TextPartDelta) and ev.delta.content_delta:
                if acc is not None:
                    acc.text.append(ev.delta.content_delta)
                await emit(
                    StreamEvent("token", chat_id, {"text": ev.delta.content_delta})
                )
            elif isinstance(ev.delta, ThinkingPartDelta) and ev.delta.content_delta:
                if acc is not None:
                    acc.thinking.append(ev.delta.content_delta)
                await emit(
                    StreamEvent("thinking", chat_id, {"text": ev.delta.content_delta})
                )
        elif isinstance(ev, FunctionToolCallEvent):
            await emit(
                StreamEvent(
                    "tool_call",
                    chat_id,
                    {"tool": ev.part.tool_name, "args": ev.part.args},
                )
            )

    async def _collect(self, chat_id: str, req: DeferredToolRequests, emit) -> dict:
        await emit(
            StreamEvent(
                "approval_request",
                chat_id,
                {
                    "calls": [
                        {"id": c.tool_call_id, "tool": c.tool_name, "args": c.args}
                        for c in req.approvals
                    ]
                },
            )
        )
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[chat_id] = fut
        try:
            decisions = await fut
        finally:
            self._pending.pop(chat_id, None)

        out: dict[str, object] = {}
        for c in req.approvals:
            v = decisions.get(c.tool_call_id, False)
            out[c.tool_call_id] = (
                ToolApproved()
                if v is True
                else ToolDenied(message=v if isinstance(v, str) else "Denied by user.")
            )
        return out
