# SPDX-License-Identifier: Apache-2.0
"""MCP server: tool registration + the approval→elicitation bridge (no browser)."""

from __future__ import annotations

import pytest

from agenticbrowser.mcp import server as mcp_server
from agenticbrowser.sdk import ApprovalRequest

_REQ = ApprovalRequest(tool="click_at", args={"label": "Pay now"}, id="c1")


async def test_tools_registered():
    tools = {t.name for t in await mcp_server.mcp.list_tools()}
    assert {"browse_task", "open_session", "close_session", "list_sessions"} <= tools


# --- fakes for the elicitation bridge ---------------------------------------
class _Data:
    def __init__(self, approve):
        self.approve = approve


class _Accept:
    action = "accept"

    def __init__(self, approve):
        self.data = _Data(approve)


class _Decline:
    action = "decline"
    data = None


class _Ctx:
    def __init__(self, result=None, raise_=False):
        self._result = result
        self._raise = raise_

    async def elicit(self, message, schema):
        if self._raise:
            raise RuntimeError("host does not support elicitation")
        return self._result


async def test_elicit_accept_allows():
    approve = mcp_server._make_approver(_Ctx(result=_Accept(True)))
    assert (await approve(_REQ)).approved is True


async def test_elicit_accept_but_false_denies():
    approve = mcp_server._make_approver(_Ctx(result=_Accept(False)))
    assert (await approve(_REQ)).approved is False


async def test_elicit_decline_denies():
    approve = mcp_server._make_approver(_Ctx(result=_Decline()))
    assert (await approve(_REQ)).approved is False


async def test_no_elicit_support_auto_denies_by_default(monkeypatch):
    monkeypatch.delenv("BROWSER_AGENT_AUTO_APPROVE", raising=False)
    approve = mcp_server._make_approver(_Ctx(raise_=True))
    verdict = await approve(_REQ)
    assert verdict.approved is False and "auto-denied" in (verdict.reason or "")


async def test_no_elicit_support_auto_approves_when_opted_in(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_AUTO_APPROVE", "true")
    approve = mcp_server._make_approver(_Ctx(raise_=True))
    assert (await approve(_REQ)).approved is True


def test_base_kwargs_from_env(monkeypatch):
    monkeypatch.setenv("BROWSER_AGENT_BACKEND", "browserbase")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj")
    monkeypatch.setenv("BROWSER_AGENT_MAX_STEPS", "25")
    kw = mcp_server._base_kwargs()
    assert kw["backend"] == "browserbase"
    assert kw["browserbase"] == {"api_key": "bb", "project_id": "proj"}
    assert kw["max_steps"] == 25
    assert kw["headless"] is True
    assert "enforce_byok" not in kw          # removed: the keys passed are the only ones used
