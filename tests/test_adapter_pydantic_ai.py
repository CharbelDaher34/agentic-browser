# SPDX-License-Identifier: Apache-2.0
"""PydanticAI adapter surface (browser-free)."""

from __future__ import annotations

import pytest

from agenticbrowser.adapters.pydantic_ai import EphemeralBrowser, browse_task_tool


def test_browse_task_tool_is_a_typed_callable():
    tool = browse_task_tool(keys={})
    assert callable(tool)
    assert tool.__name__ == "browse_task"
    # annotations are stringized (`from __future__ import annotations`)
    assert tool.__annotations__.get("goal") in (str, "str")
    assert tool.__doc__ and "browser" in tool.__doc__.lower()


def test_as_tool_requires_open_context():
    eb = EphemeralBrowser(keys={})
    with pytest.raises(RuntimeError):
        eb.as_tool()  # not entered yet
