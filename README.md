# Agentic Browser

An AI agent that **drives a real web browser** to accomplish goals you describe in
plain language — navigate, read, fill forms, click, log in, extract data — with a
built‑in **approval gate** for risky actions and **human takeover** when you want to
grab the wheel.

Use it four ways: as an **embeddable Python SDK**, as a **tool inside your own
agent**, as an **MCP server** for Claude Desktop / Cursor, or as a **self‑hosted
service** (REST + WebSockets + a full chat UI).

- Built on **PydanticAI v2** + **Playwright** (local Chromium *or* Browserbase cloud browsers)
- **Hybrid acting** — DOM mode (elements by `ref`) *and* vision mode (screenshot →
  click by pixel coordinates), so it handles canvas, maps, and visual widgets a
  DOM‑only agent misses
- **Multi‑provider** — configure an Anthropic / OpenAI / Google key (any one works end‑to‑end)
- **Apache‑2.0**, Python 3.13

> ⚠️ Use it for **legitimate, authorized** automation. It is not for defeating
> CAPTCHAs, paywalls, ToS‑prohibited scraping, or anything you're not allowed to do.

---

## Why it's different

Most browser‑agent libraries give you click/type and stop there. This one also ships
the things that make it safe to point at the real web:

- **Approval gate** — destructive actions (pay / buy / delete / send / checkout) **pause**
  and ask for a decision before executing. Fail‑safe: auto‑deny unless you say yes.
- **Human takeover + live view** — watch the agent's browser and take control at any
  time; a single‑driver *lease* pauses the agent while you drive (e.g. to clear a 2FA),
  then hands back.
- **Parallel sub‑agents** — the orchestrator can delegate independent sub‑tasks, each on
  its own browser tab, and collect concise results.
- **Resumable, auditable sessions** — cookies (`storage_state`) + full message history +
  a per‑step screenshot trail persist, so "log in once, run many tasks" works and every
  run is replayable.
- **Any single provider** — works end‑to‑end with only an Anthropic *or* OpenAI
  *or* Google key (the model auto‑resolves to whichever provider you hold a key for).

---

## How to use it

| You want to… | Use | Install |
|---|---|---|
| Call the agent from your **Python** code | **SDK** (`BrowserAgent`) | `uv add "git+https://github.com/CharbelDaher34/agentic-browser.git"` |
| Give your **own agent** a "browse the web" tool | **PydanticAI adapter** | `uv add "git+https://github.com/CharbelDaher34/agentic-browser.git"` |
| Use it in **Claude Desktop / Cursor / an MCP host** | **MCP server** | `uv add "agenticbrowser[mcp] @ git+https://github.com/CharbelDaher34/agentic-browser.git"` |
| **Self‑host** the full product (UI + multi‑user API) | **Docker** | `make up` |

> **Not yet on PyPI — install from GitHub** with `uv add` (the git URLs above).
> Developing on a clone instead? `uv sync` (adds `--extra server` / `--extra mcp` for those paths).
> The local browser backend needs Chromium once: `uv run python -m agenticbrowser.install`
> (or use `backend="browserbase"`).

### 1) Python SDK — embed it in your process

```python
import asyncio
from agenticbrowser import BrowserAgent

async def main():
    async with BrowserAgent(keys={"anthropic": "sk-ant-..."}) as agent:
        result = await agent.run("find the cheapest direct LON->NYC next Friday")
        print(result.output)     # the answer
        print(result.usage)      # {steps, requests, input_tokens, output_tokens, ...}
        print(len(result.steps)) # screenshot/action replay trail

asyncio.run(main())
```

Stream events, gate destructive actions, persist across turns:

```python
from agenticbrowser import BrowserAgent, Approval, ApprovalRequest

async def approve(req: ApprovalRequest) -> Approval:
    return Approval.allow() if "checkout" not in str(req.args) else Approval.deny("not authorized")

async with BrowserAgent(
    keys=BrowserAgent.keys_from_env(),     # ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
    approve=approve,                        # omit -> destructive actions auto-denied (fail-safe)
    persist="sqlite:///runs.db",            # resumable; omit for in-memory
    max_steps=40, max_tokens=200_000,       # budgets
    subagents=True,                         # allow parallel sub-agent tabs
) as agent:
    async for ev in agent.stream("research the top 3 competitors' pricing"):
        if ev.type == "token": print(ev.data["text"], end="")
        elif ev.type == "final": print("\n", ev.data["text"])
```

→ Full reference: **[docs/sdk.md](docs/sdk.md)**

### 2) PydanticAI adapter — a browser tool for your agent

Give an agent you're already building "hands on the web" as **one tool call**:

```python
from pydantic_ai import Agent
from agenticbrowser.adapters.pydantic_ai import EphemeralBrowser

async with EphemeralBrowser(keys={"anthropic": "sk-ant-..."}) as browser:
    planner = Agent("anthropic:claude-sonnet-4-6", tools=[browser.as_tool()])
    out = await planner.run("Research X on the web and summarize")
    print(out.output)
```

`browser.as_tool()` exposes `browse_task(goal)` — a complete autonomous browsing run
behind a single tool call (the approval gate runs inside it). The same thin‑wrapper
pattern is how LangChain / CrewAI / etc. adapters slot in.

### 3) MCP server — for Claude Desktop, Cursor, any MCP host

```json
{
  "mcpServers": {
    "agentic-browser": {
      "command": "uvx",
      "args": ["--from", "agenticbrowser[mcp] @ git+https://github.com/CharbelDaher34/agentic-browser.git", "agenticbrowser-mcp"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-...", "BROWSER_AGENT_BACKEND": "local" }
    }
  }
}
```

The host gets `browse_task(goal)` plus resumable session tools; destructive actions
surface as **MCP elicitation** (the host approves). → **[docs/mcp.md](docs/mcp.md)**

### 4) Self‑host the full app (Docker)

The whole product — REST + WebSockets + a bundled React chat UI + Postgres — in
containers, callable from any language. The Docker files live in [infra/](infra/);
a root [Makefile](Makefile) wraps the common operations:

```bash
make up        # build + start app + Postgres on http://localhost:8000  (UI + API)
# equivalently: docker compose -f infra/docker-compose.yml up --build
```

`SERVE_UI=false` runs it as a **pure API** (no UI). Keys stay in your `.env`, on your infra.
→ **[docs/self-host.md](docs/self-host.md)**

### Run the full app locally (dev)

```bash
make run        # Postgres (docker) + backend + built UI on http://localhost:8000
make dev        # hot-reload Vite UI on :5173 (proxying the API on :8000)
```

(These wrap [infra/run.sh](infra/run.sh) — run `make help` for all targets.)

Open the app, register, create a **browser session** → a **chat**, and tell the agent
what to do (e.g. *"go to Hacker News and summarize the top thread"*).

---

## Configuration (keys & models)

Settings come from the environment / `.env` (server) or constructor args (SDK).

| What | SDK arg | Server env | Default |
|---|---|---|---|
| Provider keys | `keys={...}` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | — |
| Orchestrator model | `model=` | `AGENT_MODEL` | `anthropic:claude-sonnet-4-6` |
| Sub‑agent model | `worker_model=` | `WORKER_MODEL` | = orchestrator |
| Browser backend | `backend=` | `BROWSER_PROVIDER` | `local` (or `browserbase`) |
| Budgets | `max_steps=`, `max_tokens=` | — | none |

The **SDK** takes keys explicitly via `keys=` — those are the only keys it uses; it never
falls back to a stray ambient env var. The **self-host server** uses the provider keys from
its own `.env` for every session — there is no per-session key entry.

There are **no `fast`/`smart`/`deep` tiers** — you pick a planner model and (optionally)
a cheaper `worker_model` for the focused sub‑agents. A session with only one provider's
key works end to end (the model auto‑resolves to the provider you hold a key for).

---

## How it works

```
agenticbrowser/              # core + SDK — installable on its own, no FastAPI/Postgres needed
  agent.py        PydanticAI v2 hybrid agent: orchestrator + sub-agents, DOM + vision tools
  runner.py       streamed turn loop: tokens/steps/approvals + usage + persistence
  registry.py     long-lived browser sessions + per-tab driver lease (human takeover)
  session.py      PlaywrightSession: perceive, act/verify, screencast
  providers.py    local Playwright (stealth) vs Browserbase — one interface
  models_registry.py  provider-key resolution + model construction (build/resolve_model)
  config.py       CoreConfig (dependency-free runtime config)
  stores.py       Store protocol + MemoryStore + SqliteStore (no DB required)
  artifacts.py    screenshot sinks (Local / Null / Memory)
  events.py       StreamEvent — the frozen, versioned wire contract
  sdk.py          BrowserAgent (the embeddable API)
  adapters/       framework adapters (pydantic_ai)
  mcp/            MCP server
  server/         the full gateway (extra: [server])
    gateway.py    FastAPI REST + chat WS + live-view/takeover WS + static UI
    store_sql.py  SQLModel/Postgres store (users, auth, chats, messages, steps)
    auth.py settings.py evals.py
frontend/         React (Vite) chat UI, served single-origin by the gateway
infra/            Dockerfile · docker-compose.yml · run.sh  (self-host + local dev)
Makefile          operations wrapper over infra/ (make up / run / dev / down …)
docs/             sdk.md · mcp.md · self-host.md
```

**The streaming seam.** Everything rides one event type (`StreamEvent`): the SDK's
`stream()`, the gateway's `/ws/chat`, and MCP progress all emit the same frozen,
versioned contract.

**Embeddable core.** The agent runs headless with an in‑memory or SQLite store — no
Postgres, no `.env`, no server. The full multi‑user product (`server/`) is the same
core with auth, Postgres, and the UI layered on top.

---

## When *not* to use it

Be honest with yourself — a browser‑driving LLM is the wrong tool for:

- High‑volume scraping when the site has a **clean API or static HTML** (use that — it's
  orders of magnitude faster/cheaper).
- **Deterministic regression tests** on an app you control (hand‑written Playwright/Cypress wins).
- **Sub‑second** latency needs (per‑step model round‑trips add seconds).
- **Fully autonomous spending at scale** with no oversight (it's deliberately built to pause).
- **Thousands of concurrent browsers** (each session is a heavyweight, stateful browser).

---

## Development

```bash
uv sync                                   # deps
uv run playwright install chromium        # local backend
PYTHONPATH="$PWD" uv run pytest -q        # tests (some E2E launch real Chromium)
```

Tests cover the store conformance (Memory ≡ SQLite), explicit-key enforcement (the SDK
never reads a stray env key), the frozen event contract, and real‑Chromium SDK/MCP runs
(scripted, no LLM key needed).

## License

[Apache‑2.0](LICENSE). Portions of the action vocabulary / anti‑automation hardening were
adapted from a Computer‑Use reference — see [NOTICE](NOTICE).
