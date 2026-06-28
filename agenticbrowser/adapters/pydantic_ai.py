# SPDX-License-Identifier: Apache-2.0
"""PydanticAI adapter — give any PydanticAI agent a real browser as one tool.

Recommended (long-lived browser, reused across tool calls):

    from pydantic_ai import Agent
    from agenticbrowser.adapters.pydantic_ai import EphemeralBrowser

    async with EphemeralBrowser(keys={"anthropic": "sk-..."}) as browser:
        planner = Agent("anthropic:claude-sonnet-4-6", tools=[browser.as_tool()])
        result = await planner.run("Find the cheapest direct SFO->JFK next Friday")
        print(result.output)

One-shot convenience (opens + closes a browser per call):

    planner = Agent(model, tools=[browse_task_tool(keys=...)])

The exposed tool is `browse_task(goal: str) -> str`: a complete autonomous
browsing run (hybrid DOM+vision) behind ONE tool call, so the host model spends
one round-trip instead of fifty. The destructive-action approval gate runs INSIDE
the browse_task run; pass `approve=` to decide (omitted => auto-deny, fail-safe).

Deferred (PACKAGING_PLAN.md §4 1b): a fine-grained `BrowserToolset` exposing the
13 low-level tools with approvals propagating natively as DeferredToolRequests in
the host's own run — that needs RunContext bridging and lands later.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..sdk import ApproveFn, BrowserAgent


class EphemeralBrowser:
    """A browser session held open for the lifetime of the context manager, so
    repeated `browse_task` calls reuse one Chromium/Browserbase session."""

    def __init__(self, **kwargs: Any) -> None:
        # kwargs are forwarded verbatim to BrowserAgent (keys, backend, approve, ...)
        self._kwargs = kwargs
        self._agent: BrowserAgent | None = None

    async def __aenter__(self) -> "EphemeralBrowser":
        self._agent = await BrowserAgent(**self._kwargs).__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._agent is not None:
            await self._agent.__aexit__(*exc)
            self._agent = None

    def as_tool(self) -> Callable[[str], Awaitable[str]]:
        """A `browse_task(goal)` async callable to drop into `Agent(tools=[...])`."""
        agent = self._agent
        if agent is None:
            raise RuntimeError("Enter the EphemeralBrowser context before calling as_tool().")

        async def browse_task(goal: str) -> str:
            """Drive a real web browser to accomplish `goal` (navigate, read, fill,
            click). Returns the final result/answer as text."""
            result = await agent.run(goal)
            return result.output

        return browse_task


def browse_task_tool(*, approve: ApproveFn | None = None, **kwargs: Any) -> Callable[[str], Awaitable[str]]:
    """A self-managing `browse_task(goal)` tool that opens and closes a fresh
    browser for each call. Convenient for one-offs; for multiple calls prefer
    `EphemeralBrowser` so the session (and any login) is reused."""

    async def browse_task(goal: str) -> str:
        """Drive a real web browser to accomplish `goal` (navigate, read, fill,
        click). Returns the final result/answer as text."""
        async with BrowserAgent(approve=approve, **kwargs) as agent:
            result = await agent.run(goal)
            return result.output

    return browse_task
