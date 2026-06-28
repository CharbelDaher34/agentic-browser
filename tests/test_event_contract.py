# SPDX-License-Identifier: Apache-2.0
"""Freeze the StreamEvent wire contract. This snapshot fails CI whenever an event
type or a `data` payload key is added/renamed/removed without a deliberate update —
the signal to bump EVENT_SCHEMA_VERSION and notify every consumer (SDK / CLI / MCP /
webhook / React embed)."""

from __future__ import annotations

import typing

from agenticbrowser.events import EVENT_DATA_KEYS, EVENT_SCHEMA_VERSION, EventType

EXPECTED_VERSION = "1.1"

EXPECTED_DATA_KEYS = {
    "token": {"text"},
    "thinking": {"text"},
    "tool_call": {"tool", "args"},
    "action": {"action", "ref", "target", "agent", "tab"},
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


def test_event_schema_version_frozen():
    assert EVENT_SCHEMA_VERSION == EXPECTED_VERSION


def test_event_data_keys_snapshot():
    assert EVENT_DATA_KEYS == EXPECTED_DATA_KEYS


def test_event_type_union_matches_data_keys():
    union = set(typing.get_args(EventType))
    assert union == set(EVENT_DATA_KEYS), "EventType Literal and EVENT_DATA_KEYS must agree"
