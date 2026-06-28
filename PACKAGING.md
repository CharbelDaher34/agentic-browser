# Packaging & Distribution Strategy

How to package this agentic browser so other developers can easily download and
integrate it — both **with a frontend** (human-in-the-loop) and **headless**
(automation). This is a strategy/brainstorm doc, not a commitment.

---

## The one insight that drives everything

The hard part is already done:

- **Streaming is one clean seam** — `emit: Callable[[StreamEvent], Awaitable[None]]`
  (see [agent.py](app/agent.py), [runner.py](app/runner.py)). Every consumer
  (UI, CLI, webhook, MCP progress) is just another `emit` sink.
- **The browser backend is already swappable** — local Playwright vs Browserbase
  behind one interface in [providers.py](app/providers.py).

The **one blocker** to "download and integrate": the agent core is welded to
Postgres (`Runner → Registry → Recorder → Store`) and to a global `settings()`
singleton that reads `.env` on import. So `import agenticbrowser` today would try
to reach a DB and could resolve a stray env key — silently using a key the caller
never passed.

**One ~M-sized refactor unlocks 4 of the 7 form-factors below.** See
[The unifying refactor](#the-unifying-refactor).

---

## The 7 form-factors, by how a developer consumes it

### Group 1 — As code in their own process (share ONE core refactor)

| # | Form-factor | DX | Audience | Frontend | Effort |
|---|---|---|---|---|---|
| 1 | **Python SDK** | `async with BrowserAgent(...) as a: await a.run("book a table")` | Python backends, AI builders | optional/none | M |
| 2 | **Framework adapters** | `Agent(toolset=BrowserToolset(browser))` — PydanticAI / LangChain / CrewAI / Temporal | teams on an agent stack | none | S→L |
| 3 | **MCP server** | `uvx agenticbrowser-mcp` → `browse_task(goal)` in Claude Desktop / Cursor | MCP-host builders | none | L |

> A standalone CLI was considered and **dropped** — the SDK + MCP server cover
> scripting/automation needs.

One library + thin wrappers. The SDK is the foundation; the PydanticAI adapter is
near-zero code (the tools in [agent.py](app/agent.py) are *already* PydanticAI v2
functions); the MCP server is a FastMCP wrapper over it.

**Standout vs. browser-use / Stagehand / Playwright-MCP:** those give click/type
with no human in the loop. This carries the **approval gate** (pay/buy/delete
pauses the run) and **live-view + takeover** as first-class hooks — MCP gets
approval-as-*elicitation*; `browse_task(goal)` is one call instead of ~50
low-level round-trips.

**SDK sketch:**

```python
from agenticbrowser import BrowserAgent, Approval, ApprovalRequest

async def approve(req: ApprovalRequest) -> Approval:
    return Approval.allow() if "checkout" not in str(req.args) else Approval.deny("not authorized")

async with BrowserAgent(
    keys=BrowserAgent.keys_from_env(),
    approve=approve,                 # omit -> auto-DENY destructive (fail safe)
    subagents=True,
    persist="sqlite:///runs.db",     # or None (in-memory) or a custom Store
) as agent:
    async for ev in agent.stream("book a table for 2 at 7pm tomorrow"):
        if ev.type == "token": print(ev.data["text"], end="")
        elif ev.type == "final": print("\nDONE:", ev.data["text"])
```

### Group 2 — As a service they host (needs NO core refactor — ships the full gateway)

| # | Form-factor | DX | Audience | Frontend | Effort |
|---|---|---|---|---|---|
| 5 | **Self-host Docker** ("Browserbox") | `docker compose up` → REST+WS, typed TS/Py clients, webhooks | non-Python stacks, VPC/air-gapped | bundled, `SERVE_UI` flag | M |

[gateway.py](app/gateway.py) already serves the React build single-origin, so the
image bakes `frontend/dist` and a flag toggles the UI. Add `POST
/api/chats/{id}/runs` (fire-and-forget) + an HMAC-signed webhook sink (just
another `emit` target). Cost: stateful container (Chromium `shm_size`, in-memory
sessions → sticky routing).

### Group 3 — As a UI component (no core refactor — rides the wire contract)

| # | Form-factor | DX | Audience | Frontend | Effort |
|---|---|---|---|---|---|
| 6 | **Embeddable widget** | `<AgentBrowser baseUrl token chatId/>` (React) / web component / `<iframe>` | product/frontend devs, any framework | **is** the frontend | M |

Extract `frontend/src/chat/*` + `LivePanel` (screencast canvas + pixel-accurate
takeover + inline approval modal + step trail) into `@agenticbrowser/react` + a
web component + an iframe embed. The work is severing `api.js`'s hardcoded
token / `location.host` / theme into a context provider — after which the existing
frontend consumes the package too (single source of truth).

### Group 4 — They run nothing (a business, not a package)

| # | Form-factor | DX | Audience | Frontend | Effort |
|---|---|---|---|---|---|
| 7 | **Hosted SaaS API** | `POST /v1/tasks {goal}` → sync or webhook, per-task billing | app builders wanting zero infra | optional dashboard + embed iframe | L + ops |

Browserbox + orgs / api-keys / quota / billing / KMS / SSRF-isolation. Defer until
the OSS path has pull.

---

## Use cases (with-frontend vs headless)

**With a frontend — the human-in-the-loop is the point:**
- Back-office co-pilot on legacy admin panels / vendor portals with no API.
- Procurement / booking copilot — assembles the cart, **stops at checkout** for approval.
- Support agent-assist (fintech/telco) — drives internal CRM, money moves need approval.
- Accessibility "do-it-for-me" layer; vision mode handles unlabeled controls.

**Without a frontend — headless automation:**
- Competitive/price monitoring on JS/canvas/map sites that **beat DOM-only scrapers** (vision mode + `locate()`).
- Resilient RPA replacing selector-based UiPath/Blue Prism flows.
- Compliance evidence capture — the per-step screenshot trail in [recorder.py](app/recorder.py) as a tamper-evident audit record.
- Synthetic monitoring / CI smoke; agent-eval harnesses swapping providers via the model + key.
- AI agent "hands on the web" — another LLM app calls `browse_task()`.

**Anti-patterns (wrong tool):** high-volume scraping when a clean API/static HTML
exists; deterministic regression tests on a stable app you control; sub-second
SLAs; fully-autonomous spending at scale (fights the safety design); thousands of
concurrent browsers (each session is heavyweight).

---

## The unifying refactor

> Kill the `from .config import settings` global and the concrete-`Store` import;
> replace both with injected dependencies.

1. **`CoreConfig` dataclass** (headless, tab/subagent limits, server keys,
   browserbase creds) threaded via `AgentDeps`, so `agent.py`,
   `providers.py`, `registry.py`, `models_registry.py` stop calling `settings()`
   at runtime. `Settings` becomes one producer of a `CoreConfig`.
2. **Narrow `Store` Protocol** — the core touches only ~11 methods (registry:
   `load_storage_state`/`upsert_session`/`load_bb_session_id`/…;
   runner: `max_step_idx`/`load_messages`/`save_messages`/…; recorder:
   `insert_step`). Keys are NOT a store concern — they live in `CoreConfig`. Ship
   `MemoryStore` + `SqliteStore`. The SQLModel class keeps all auth/chat/user methods
   and stays gateway-only.
3. **`ArtifactStore` is already a Protocol** ([recorder.py](app/recorder.py)) — just
   add `NullArtifacts` / `MemoryArtifacts`. Recorder & registry need only type-hint swaps.
4. **Make the provider-key dict injectable** in
   [models_registry.py](app/models_registry.py) — *today it can resolve a stray
   env key the caller never passed.* Keys come ONLY from `CoreConfig.provider_keys`
   (the SDK's `keys=`, or the server's .env) — no per-session key, no ambient-env
   fallback. Fix in the same PR.

Result: `import agenticbrowser` works with no `.env` and no Postgres. Unlocks the
SDK, adapters, MCP server, and CLI from one PR. The other three (Browserbox,
embed, SaaS) don't need it.

---

## Roadmap

- **Phase 0 — Prove the seam (1 PR, make-or-break):** CoreConfig + Store Protocol +
  Memory/SQLite impls + injectable provider keys + a conformance test (same scripted run
  against MemoryStore and SqliteStore → identical step trail / message history).
- **Phase 1 — Ship the core:** `BrowserAgent` SDK **+** PydanticAI `BrowserToolset`
  adapter as one workstream. Add an `agenticbrowser install` step for Playwright
  Chromium; advertise Browserbase as the no-local-browser path.
- **Phase 2 — Distribution channels:** MCP server, then CLI, then the long adapter
  tail (LangChain/CrewAI/Temporal) as demand appears.
- **Phase 3 — Service & UI (independent track, no core refactor):** Browserbox
  Docker + embeddable widget. Defer SaaS.

---

## Gaps to decide early

- **OSS/license model is unchosen** and gates the portfolio (Apache-2.0 for
  adoption vs AGPL/BSL to protect a future hosted product).
- **The approval gate is a regex** (`_DESTRUCTIVE_RE`: pay/buy/delete/send/checkout)
  — misses coordinate clicks with no label and localized/novel verbs ("Confirmer",
  "Place order"). Needs spend caps + a model-assisted classifier for the
  "safe enough to spend money" pitch.
- **Prompt-injection / SSRF** — every backend embed (not just SaaS) needs an egress
  allowlist + untrusted-content framing.
- **Freeze + version the `StreamEvent` contract** — it's the shared wire format
  across SDK/CLI/MCP/webhooks/embed; add a CI test that fails on a renamed/dropped event.
- **Cost/observability** — the agent silently burns API budget inside one `run()`;
  surface step/token/$ caps + OpenTelemetry spans.
- **Sandboxing** — "embed in your Celery worker" means arbitrary web JS runs next to
  app secrets; recommend containerized Chromium by default.

---

## Recommendation

Do **Phase 0 + the SDK + PydanticAI adapter** first. It's the smallest change that
makes the project genuinely embeddable *and* lands the true differentiator (native
approval gate + human takeover) in the stack this audience already uses. Everything
else (MCP, CLI, Docker, widget, SaaS) is a thin wrapper or an independent track on
top of that core.
