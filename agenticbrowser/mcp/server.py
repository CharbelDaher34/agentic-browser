# SPDX-License-Identifier: Apache-2.0
"""MCP server — give any MCP host (Claude Desktop, Cursor, another agent) a real
browser as one capability.

The headline tool is `browse_task(goal)`: a complete autonomous browsing run
(hybrid DOM+vision, parallel sub-agents) behind ONE MCP call, so the host spends
one round-trip instead of fifty. Named, resumable sessions (`open_session`) let a
host log in once and reuse the authenticated browser across calls.

The destructive-action approval gate becomes MCP **elicitation**: when the agent
hits a pay/buy/delete/checkout step, the host is asked to approve. If the host
can't elicit, the action is auto-denied (fail-safe) unless
`BROWSER_AGENT_AUTO_APPROVE=true`.

Run it:
    uvx agenticbrowser-mcp            # stdio (Claude Desktop default)
    BROWSER_AGENT_MCP_TRANSPORT=streamable-http uvx agenticbrowser-mcp   # remote

Config via env: ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY,
BROWSER_AGENT_BACKEND=local|browserbase (+ BROWSERBASE_API_KEY / _PROJECT_ID),
AGENT_MODEL, BROWSER_AGENT_HEADLESS, BROWSER_AGENT_SUBAGENTS,
BROWSER_AGENT_MAX_STEPS, BROWSER_AGENT_AUTO_APPROVE.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel

from ..sdk import Approval, ApprovalRequest, BrowserAgent

mcp = FastMCP(
    "agentic-browser",
    instructions=(
        "Drive a real web browser to accomplish goals. Use `browse_task(goal)` for a "
        "one-shot run, or `open_session()` then `browse_task(goal, session_id=...)` to "
        "reuse a logged-in browser across calls. Destructive actions ask for approval."
    ),
)

# persistent sessions reused across browse_task calls (key = BrowserAgent.session_id).
# Bounded LRU: beyond _MAX_SESSIONS the oldest is closed (a host that forgets to
# call close_session would otherwise leak live browsers for the process lifetime).
_SESSIONS: dict[str, BrowserAgent] = {}
_MAX_SESSIONS = 16


class _ApprovalDecision(BaseModel):
    approve: bool = False


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _base_kwargs() -> dict:
    backend = os.environ.get("BROWSER_AGENT_BACKEND", "local")
    kw: dict = {
        "keys": BrowserAgent.keys_from_env(),
        "backend": backend,
        "headless": _env_bool("BROWSER_AGENT_HEADLESS", True),
        "subagents": _env_bool("BROWSER_AGENT_SUBAGENTS", False),
    }
    if os.environ.get("AGENT_MODEL"):
        kw["model"] = os.environ["AGENT_MODEL"]
    if os.environ.get("WORKER_MODEL"):
        kw["worker_model"] = os.environ["WORKER_MODEL"]
    if backend == "browserbase":
        kw["browserbase"] = {
            "api_key": os.environ.get("BROWSERBASE_API_KEY"),
            "project_id": os.environ.get("BROWSERBASE_PROJECT_ID"),
        }
    if os.environ.get("BROWSER_AGENT_MAX_STEPS"):
        kw["max_steps"] = int(os.environ["BROWSER_AGENT_MAX_STEPS"])
    return kw


def _make_approver(ctx: Context):
    """An approve= handler that asks the MCP host via elicitation, falling back to
    the env default (deny, unless BROWSER_AGENT_AUTO_APPROVE) when the host can't."""
    auto_approve = _env_bool("BROWSER_AGENT_AUTO_APPROVE", False)

    async def approve(req: ApprovalRequest) -> Approval:
        try:
            result = await ctx.elicit(
                message=f"The agent wants to perform a destructive action: "
                f"`{req.tool}` with {req.args}. Approve?",
                schema=_ApprovalDecision,
            )
        except Exception:  # noqa: BLE001 — host doesn't support elicitation
            return (
                Approval.allow()
                if auto_approve
                else Approval.deny(
                    "auto-denied (host cannot elicit; set BROWSER_AGENT_AUTO_APPROVE=true to allow)"
                )
            )
        if result.action == "accept" and getattr(result, "data", None) and result.data.approve:
            return Approval.allow()
        return Approval.deny(f"{result.action} by user")

    return approve


@mcp.tool()
async def browse_task(goal: str, ctx: Context, session_id: str = "") -> str:
    """Drive a real web browser to accomplish `goal` (navigate, read, fill, click)
    and return the result as text. Pass a `session_id` from open_session to reuse a
    logged-in browser; omit it for a one-shot run."""
    approve = _make_approver(ctx)  # per-call handler bound to THIS request's ctx
    if session_id and session_id in _SESSIONS:
        _SESSIONS[session_id] = _SESSIONS.pop(session_id)  # LRU touch (mark recently used)
        result = await _SESSIONS[session_id].run(goal, approve=approve)
        return result.output
    async with BrowserAgent(**_base_kwargs()) as agent:
        result = await agent.run(goal, approve=approve)
        return result.output


@mcp.tool()
async def open_session() -> str:
    """Open a persistent browser session that survives across browse_task calls
    (so a login is reused). Returns its session_id; close it with close_session."""
    while len(_SESSIONS) >= _MAX_SESSIONS:  # LRU-evict + close the oldest
        old_id, old_agent = next(iter(_SESSIONS.items()))
        _SESSIONS.pop(old_id, None)
        try:
            await old_agent.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
    agent = await BrowserAgent(**_base_kwargs()).__aenter__()
    _SESSIONS[agent.session_id] = agent
    return agent.session_id


@mcp.tool()
async def close_session(session_id: str) -> str:
    """Close a session previously opened with open_session."""
    agent = _SESSIONS.pop(session_id, None)
    if agent is None:
        return f"No such session: {session_id}"
    await agent.__aexit__(None, None, None)
    return f"closed {session_id}"


@mcp.tool()
async def list_sessions() -> list[str]:
    """List the ids of currently-open persistent sessions."""
    return list(_SESSIONS)


def main() -> None:
    transport = os.environ.get("BROWSER_AGENT_MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
