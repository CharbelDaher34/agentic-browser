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
    DeferredToolRequests,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)
from pydantic_ai.tools import DeferredToolResults, ToolApproved, ToolDenied

from .agent import AgentDeps, agent
from .models import StreamEvent
from .recorder import Recorder
from .registry import SessionRegistry
from .store import Store


class Runner:
    def __init__(
        self, registry: SessionRegistry, store: Store, recorder: Recorder
    ) -> None:
        self.registry = registry
        self.store = store
        self.recorder = recorder
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._inflight: set[str] = set()
        self._inflight_lock = asyncio.Lock()

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

    async def run_turn(
        self, session_id: str, chat_id: str, user_text: str, emit
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
                emit, _idx=[start_idx],
            )
            history = await self.store.load_messages(chat_id)
            prompt: str | None = user_text
            deferred: DeferredToolResults | None = None

            while True:
                result = None
                async with agent.run_stream_events(
                    prompt,
                    deps=deps,
                    message_history=history,
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
                try:
                    state = await self.registry.get(session_id).storage_state()
                    await self.store.save_storage_state(session_id, state)
                except Exception:  # noqa: BLE001
                    pass
                await emit(StreamEvent("final", chat_id, {"text": result.output}))
                return result.output
        except asyncio.CancelledError:
            # the approval future was cancelled (chat WS dropped mid-approval);
            # let it propagate after the finally releases the lease.
            raise
        except Exception as exc:  # noqa: BLE001
            await emit(StreamEvent("error", chat_id, {"msg": f"Agent error: {exc}"}))
            return ""
        finally:
            if lease is not None:
                await self.registry.release(session_id, lease.token)
            self._inflight.discard(chat_id)

    async def _on_event(self, chat_id: str, ev, emit) -> None:
        if isinstance(ev, PartDeltaEvent):
            if isinstance(ev.delta, TextPartDelta) and ev.delta.content_delta:
                await emit(
                    StreamEvent("token", chat_id, {"text": ev.delta.content_delta})
                )
            elif isinstance(ev.delta, ThinkingPartDelta) and ev.delta.content_delta:
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
        elif isinstance(ev, FunctionToolResultEvent):
            await emit(
                StreamEvent("tool_result", chat_id, {"tool_call_id": ev.tool_call_id})
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
