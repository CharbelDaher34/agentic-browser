# SPDX-License-Identifier: Apache-2.0
"""The StreamEvent wire contract — frozen and versioned (W-B).

`StreamEvent` is the single streaming seam every consumer rides: the SDK
`stream()`, the gateway's `/ws/chat` plane, and MCP progress notifications. Because
so many surfaces depend on it, the shape is FROZEN and VERSIONED here:

  • `EVENT_SCHEMA_VERSION` is bumped per the policy below.
  • `EVENT_DATA_KEYS` documents the `data` payload keys for every event type and is
    snapshot-tested (`tests/test_event_contract.py`) so a rename/drop in
    runner.py / agent.py / gateway.py fails CI unless the version is bumped.

Versioning policy: ADD a field or event type -> bump MINOR; RENAME or REMOVE a
field/type -> bump MAJOR. Consumers negotiate via `protocolVersion` (the server
returns it from /api/config; the SDK/MCP expose it at their handshake).

Note on planes: `lease` and `live_view` are emitted on the *view* WebSocket
(`/ws/view`) as plain JSON control messages, NOT as chat-plane StreamEvents — they
are listed here so the cross-surface contract is complete in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

EVENT_SCHEMA_VERSION = "1.0"

EventType = Literal[
    "token",            # incremental assistant text
    "thinking",         # incremental reasoning text
    "tool_call",        # the model invoked a tool
    "action",           # an agent action started on a tab
    "observation",      # the result/observation after an action
    "approval_request", # destructive action paused for human approval
    "final",            # the turn's final answer
    "error",            # a turn-level error
    "interrupted",      # the turn was interrupted (steering / disconnect)
    "subagent_start",   # a delegated sub-agent began
    "subagent_end",     # a delegated sub-agent finished
    "usage",            # cost/usage report for the turn (W-C)
    "lease",            # [view plane] driver/takeover state for a tab
    "live_view",        # [view plane] live-view mode + url
]

# The expected `data` payload keys per event type. Snapshot-tested for drift.
# Keys marked optional (present-sometimes) are suffixed with "?".
EVENT_DATA_KEYS: dict[str, set[str]] = {
    "token": {"text"},
    "thinking": {"text"},
    "tool_call": {"tool", "args"},
    "action": {"action", "ref", "agent", "tab"},
    "observation": {"idx", "url", "title", "ok", "changed", "agent", "tab"},
    "approval_request": {"calls"},
    "final": {"text"},
    "error": {"msg"},
    "interrupted": {"text"},
    "subagent_start": {"id", "task", "model", "tab"},
    "subagent_end": {"id", "result", "ok"},
    "usage": {"steps", "requests", "input_tokens", "output_tokens", "total_tokens", "cost_usd"},
    "lease": {"granted", "driver", "tab_id"},
    "live_view": {"mode", "url"},
}


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One ordered streaming event. `data` shape per `EVENT_DATA_KEYS[type]`."""

    type: EventType
    chat_id: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def wire(self) -> dict:
        """The `{type, data}` JSON envelope the `/ws/chat` plane sends."""
        return {"type": self.type, "data": dict(self.data)}
