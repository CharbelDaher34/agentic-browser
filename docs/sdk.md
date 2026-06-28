# Agentic Browser — Python SDK

Drive the agent headlessly from your own process. No FastAPI, no Postgres, no
frontend. You pass your own LLM provider key.

## Install

```bash
# not yet on PyPI — install from GitHub
uv add "git+https://github.com/CharbelDaher34/agentic-browser.git"
uv run python -m agenticbrowser.install   # one-time: fetch Chromium for the local backend
```

`python -m agenticbrowser.install` (or the `agenticbrowser-install` script) wraps
`playwright install chromium`. Skip it if you only use `backend="browserbase"`.

## Quickstart

```python
import asyncio
from agenticbrowser import BrowserAgent

async def main():
    async with BrowserAgent(keys={"anthropic": "sk-ant-..."}) as agent:
        result = await agent.run("find the cheapest direct LON->NYC next Friday")
        print(result.output)        # the final answer
        print(result.usage)         # {steps, requests, input_tokens, output_tokens, total_tokens, cost_usd}
        print(len(result.steps))    # the screenshot/action replay trail

asyncio.run(main())
```

`keys` are your own provider keys (Anthropic / OpenAI / Google). `BrowserAgent.keys_from_env()`
reads `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`.

## Streaming

```python
async with BrowserAgent(keys=BrowserAgent.keys_from_env()) as agent:
    async for ev in agent.stream("summarize today's top Hacker News thread"):
        if ev.type == "token":   print(ev.data["text"], end="")
        elif ev.type == "action": print("\n>>", ev.data["action"], ev.data.get("tab"))
        elif ev.type == "final":  print("\n", ev.data["text"])
```

Event shapes are the frozen, versioned contract in `agenticbrowser.events`
(`EVENT_SCHEMA_VERSION`, `EVENT_DATA_KEYS`).

## Approval gate (safety)

Destructive actions (pay / buy / delete / send / checkout) **pause** the run.

```python
from agenticbrowser import Approval, ApprovalRequest

async def approve(req: ApprovalRequest) -> Approval:
    print("agent wants:", req.tool, req.args)
    return Approval.allow() if "checkout" not in str(req.args) else Approval.deny("not authorized")

async with BrowserAgent(keys=..., approve=approve) as agent:
    ...
```

- Omit `approve=` and destructive actions are **auto-denied** (fail-safe).
- An `approve` handler may return an `Approval`, or `True` (allow), or a `str`
  (deny with that reason). Sync or async.

## Persistence (resumable sessions)

```python
# in-memory (default): ephemeral — lost on process exit
BrowserAgent(keys=...)

# SQLite: resumable conversation + replay trail, no Postgres
BrowserAgent(keys=..., persist="sqlite:///runs.db")

# or inject any object implementing the Store protocol
BrowserAgent(keys=..., persist=my_store)
```

Calling `run()` / `stream()` again on the same instance continues the
conversation (history accumulates under one chat id). `await agent.export_messages()`
returns the PydanticAI message history.

## Budgets (cost control)

```python
BrowserAgent(keys=..., max_steps=40, max_tokens=200_000)
```

Hitting a budget ends the turn with an `error` event (`"Budget exceeded: ..."`).
Per-step token counts are reported in the `usage` event / `RunResult.usage`.
(`max_cost_usd` and a $ price table land with the full cost-observability work.)

## Cloud browser (no local Chromium)

```python
BrowserAgent(
    keys={"anthropic": "sk-..."},
    backend="browserbase",
    browserbase={"api_key": "bb_...", "project_id": "proj_..."},
)
```

## Parallel sub-agents

```python
BrowserAgent(
    keys=...,
    subagents=True, max_concurrent_subagents=3, max_tabs=8,
    model="anthropic:claude-opus-4-6",       # the orchestrator (planner)
    worker_model="anthropic:claude-sonnet-4-6",  # sub-agents (defaults to model)
)
```

The orchestrator delegates independent sub-tasks to parallel tabs. Set
`worker_model` to run those focused sub-agents on a cheaper model than the planner
(omit it and they reuse `model`). There are no fast/smart/deep tiers — you choose
the two models explicitly.

## Use it as a tool in your own PydanticAI agent

```python
from pydantic_ai import Agent
from agenticbrowser.adapters.pydantic_ai import EphemeralBrowser

async with EphemeralBrowser(keys={"anthropic": "sk-..."}) as browser:
    planner = Agent("anthropic:claude-sonnet-4-6", tools=[browser.as_tool()])
    out = await planner.run("Research X on the web and summarize")
    print(out.output)
```

`browser.as_tool()` exposes one tool — `browse_task(goal)` — a complete autonomous
browsing run behind a single tool call. For one-offs, `browse_task_tool(keys=...)`
opens/closes a browser per call.

## Not yet in the SDK (roadmap)

Interactive human-takeover (`live_view()` / `takeover()` / `send_input()`),
mid-run `steer()`, the fine-grained 13-tool `BrowserToolset` (with approvals
propagating as native `DeferredToolRequests` in your own agent run), and a `$`
cost price table. See `PACKAGING_PLAN.md`.
