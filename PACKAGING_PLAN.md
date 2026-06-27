# Implementation Plan — OSS Packaging (Groups 1–3)

Companion to [PACKAGING.md](PACKAGING.md). Scope locked by the following decisions:

- **License: Apache-2.0** (permissive, maximize adoption).
- **No SaaS** — Group 4 (hosted multi-tenant) is dropped.
- **Ship Groups 1, 2, 3:** the embeddable core (SDK + adapters + MCP + CLI), the
  self-host Docker service, and the embeddable frontend widget.
- Four cross-cutting must-haves: **freeze+version the StreamEvent contract**, **cost
  observability**, **improve the approval gate**, **Apache-2.0 licensing**.

Everything rides one decoupled core (Phase 0). The four cross-cutting workstreams
(W-A…W-D) land alongside Phase 0 because every form-factor depends on them.

---

## 0. Dependency graph & milestones

```
                 ┌─ W-A Apache-2.0 license ─────────────┐
                 ├─ W-B StreamEvent freeze + versioning ─┤   (cross-cutting,
 Phase 0 ────────┼─ W-C Cost observability ──────────────┤    land with Phase 0)
 (core decouple) ├─ W-D Approval-gate v2 ────────────────┘
        │
        ├──► Group 1a  Python SDK (BrowserAgent)
        │        ├──► 1b  PydanticAI adapter  (near-zero code)
        │        ├──► 1c  MCP server          (FastMCP wrapper)
        │        └──► 1d  CLI                 (TTY/JSON shell)
        │
        ├──► Group 2   Docker self-host       (needs NO Phase 0; needs W-B for clients)
        │
        └──► Group 3   Embeddable widget      (needs NO Phase 0; needs W-B for TS types)
```

| Milestone | Contents | Gate |
|---|---|---|
| **M0** | W-A, Phase 0, W-B/W-C/W-D foundations | conformance test green; `import agenticbrowser` works with no `.env`/DB |
| **M1** | Group 1a SDK + 1b PydanticAI adapter | `pip install`, `await agent.run(...)` works headless |
| **M2** | Group 1c MCP + 1d CLI | `uvx` runs both; approval + usage flow through |
| **M3** | Group 2 Docker self-host | `docker compose up` → REST/WS + webhook; codegen clients |
| **M4** | Group 3 widget | `@agenticbrowser/react` + web component + iframe; old frontend consumes it |

---

## 1. Package & repo restructure (prereq for `pip install`)

The import name is currently `app` — unshippable. Target: one distribution
`agenticbrowser` with optional extras, core importable with zero server deps.

**Target layout:**

```
agenticbrowser/                 # pip-installable CORE + SDK (no FastAPI, no Postgres)
  __init__.py                   # public exports: BrowserAgent, StreamEvent, Approval, ...
  agent.py runner.py registry.py session.py providers.py     # moved from app/
  models.py models_registry.py history.py
  config.py                     # NEW CoreConfig dataclass (no pydantic-settings)
  stores.py                     # NEW Store Protocol + MemoryStore + SqliteStore
  artifacts.py                  # ArtifactStore Protocol + Null/Memory/Local (from recorder.py)
  recorder.py                   # unchanged logic, imports artifacts+stores Protocols
  events.py                     # NEW StreamEvent + EVENT_SCHEMA_VERSION + JSON Schema
  usage.py                      # NEW cost/usage accounting (W-C)
  approval.py                   # NEW approval policy + classifier v2 (W-D)
  sdk.py                        # NEW BrowserAgent (Group 1a)
  adapters/__init__.py pydantic_ai.py langchain.py crewai.py temporal.py   # Group 1b
  mcp/server.py                 # Group 1c
  cli.py                        # Group 1d
  server/                       # the GATEWAY (moved from app/, server-only)
    gateway.py auth.py crypto.py settings.py store_sql.py
frontend/                       # unchanged app; later consumes packages/react
packages/react/                 # Group 3 npm package (@agenticbrowser/react)
LICENSE NOTICE                  # W-A
```

**Migration tasks**

- [ ] `git mv app/{agent,runner,registry,session,providers,recorder,models,models_registry,history}.py agenticbrowser/`
- [ ] `git mv app/gateway.py app/auth.py app/crypto.py agenticbrowser/server/`; rename
      `app/store.py` → `agenticbrowser/server/store_sql.py` (the SQLModel/Postgres impl,
      keeps **all** user/auth/chat methods).
- [ ] Split `app/config.py`: the `Settings` (pydantic-settings) class →
      `agenticbrowser/server/settings.py`; the new `CoreConfig` → `agenticbrowser/config.py`.
- [ ] Fix imports (`.config import settings` → see Phase 0); keep relative imports inside the package.
- [ ] **Deploy shim:** `main.py` becomes `from agenticbrowser.server.gateway import app`;
      update `pyproject.toml [tool.fastapi] entrypoint` stays `main:app` (unchanged); verify
      `.fastapicloud/` build still finds it. `run.sh` uvicorn target →
      `agenticbrowser.server.gateway:app`.
- [ ] `pyproject.toml`: package discovery for `agenticbrowser*`; extras
      `[server]` (fastapi, sqlmodel, asyncpg, uvicorn, python-multipart),
      `[mcp]` (mcp/fastmcp), `[adapters]` (langchain, temporalio…), `[otel]`
      (opentelemetry). Base deps = pydantic-ai-slim, playwright, browserbase, aiosqlite.

> Lower-risk alternative if the rename is too disruptive now: keep modules in `app/`,
> add `agenticbrowser/__init__.py` that re-exports, and rename later. The plan assumes
> the full rename at M0 since it's cheapest before more files are added.

---

## 2. Phase 0 — core decoupling (the unlock)

Two changes: kill the `settings()` global in the core, and abstract `Store`.

### 2.1 `CoreConfig` dataclass — `agenticbrowser/config.py`

Plain frozen dataclass consumed by the core. `Settings` (server) produces one;
`BrowserAgent` builds one directly from constructor args.

```python
@dataclass(frozen=True)
class CoreConfig:
    # browser
    browser_provider: str = "local"          # registry.py:63,195
    headless: bool = True                     # providers.py:82
    browserbase_api_key: str | None = None    # providers.py:112 fallback
    browserbase_project_id: str | None = None
    # models / BYOK
    agent_model: str = "anthropic:claude-sonnet-4-6"   # models_registry:85,99
    enforce_byok: bool = True                 # models_registry:76,113 ; providers:112
    server_keys: dict[str, str | None] = field(default_factory=dict)  # {anthropic,openai,google}
    # agent limits
    max_subagent_depth: int = 1               # agent.py:485
    max_concurrent_subagents: int = 1         # agent.py:411,485
    max_tabs: int = 6                         # agent.py:539-540
    # lifecycle / screencast
    idle_ttl_seconds: int = 1800              # registry.py:190
    screencast_quality: int = 60              # session.py:393
    screencast_every_nth_frame: int = 1       # session.py:408
    screencast_max_width: int | None = None
    screencast_max_height: int | None = None
    # budgets (W-C)
    max_steps: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
```

**Threading tasks** (replace the 14 verified `settings()` call sites):

- [ ] `agent.py` — add `cfg: CoreConfig` to `AgentDeps`; replace `settings()` at
      :411, :485, :539-540 with `ctx.deps.cfg`. (system-prompt string at :411 is built
      per-run from `deps.cfg`.)
- [ ] `registry.py` — `SessionRegistry.__init__(store, cfg)`; replace :63 (`browser_provider`)
      and :190 (`idle_ttl_seconds`). Pass `cfg` into `make_provider(...)` and into
      `PlaywrightSession.open(...)`.
- [ ] `providers.py` — `make_provider(name, *, cfg, browserbase_creds=None)`;
      `LocalProvider(cfg)` uses `cfg.headless` (:82); `BrowserbaseProvider(cfg, ...)`
      uses `cfg.enforce_byok` + fallback creds (:112); :195 default from `cfg`.
- [ ] `session.py` — pass screencast params (`cfg`) into `PlaywrightSession`; replace :393, :408.
- [ ] `models_registry.py` — make `enforce_byok` + `server_keys` injectable. Functions
      `_server_key`, `_pick_key`, `available_providers`, `pick_model`, `build_model`,
      `build_vision_model` take a `cfg: CoreConfig` (or a small `KeyResolver` built from it).
      **This is the single most important fix:** today (:67,:76,:113) it can resolve a stray
      server key from env, contradicting `enforce_byok`. After this, an embed with
      `enforce_byok=True` and only user keys can never touch a server/env key.
- [ ] `server/gateway.py` lifespan — build `cfg = settings().to_core_config()` once; pass
      to `SessionRegistry(store, cfg)`, `Recorder(store, artifacts)`, `Runner(...)`. Add
      `Settings.to_core_config()` in `server/settings.py` (the one producer).
- [ ] Leave gateway-only `settings()` users untouched: `auth.py:51` (token_ttl),
      `crypto.py:29` (app_secret), `gateway.py` artifacts_dir/cors/registration.

### 2.2 `Store` Protocol + impls — `agenticbrowser/stores.py`

The core calls **exactly 14 methods** (grep-verified across registry/runner/recorder):

```python
class Store(Protocol):
    # registry.py
    async def load_storage_state(self, session_id) -> dict | None: ...
    async def save_storage_state(self, session_id, state, last_url=None) -> None: ...
    async def load_last_url(self, session_id) -> str | None: ...
    async def load_bb_session_id(self, session_id) -> str | None: ...
    async def save_bb_session_id(self, session_id, bb_id) -> None: ...
    async def upsert_session(self, session_id, provider) -> None: ...
    async def get_session(self, session_id) -> dict | None: ...
    async def load_session_browserbase_creds(self, session_id) -> dict | None: ...
    async def delete_session_keys(self, session_id) -> None: ...
    # runner.py
    async def load_messages(self, chat_id) -> list: ...
    async def save_messages(self, chat_id, messages) -> None: ...
    async def max_step_idx(self, chat_id) -> int: ...
    async def load_session_keys(self, session_id) -> dict[str, str]: ...
    # recorder.py
    async def insert_step(self, rec: StepRecord) -> None: ...
```

- [ ] Define the Protocol; the existing SQL class (`server/store_sql.py`) already satisfies
      it (plus its server-only user/auth/chat methods) — add `Store` to its bases for clarity.
- [ ] `MemoryStore` — dicts in RAM. `load_session_keys` / `load_session_browserbase_creds`
      return the **keys injected via constructor** (NOT a DB lookup → `crypto.py`/`auth.py`
      never imported). `save_*`/`delete_*` are no-ops or RAM writes. Messages + steps held in lists.
- [ ] `SqliteStore(path)` — `aiosqlite`, **WAL mode** (sub-agents write steps concurrently).
      Persists storage_state, bb_session_id, last_url, messages-blob, steps. Key methods
      delegate to injected keys (keys are provided per-construction, not persisted in embed mode).
- [ ] Conformance test (below) guarantees no method is missing at runtime.

### 2.3 Artifacts — `agenticbrowser/artifacts.py`

`ArtifactStore` Protocol already exists in `recorder.py:20` (`put_png`). Move it +
`LocalArtifacts` here and add:

- [ ] `NullArtifacts.put_png` → returns a synthetic key, drops bytes (headless, no disk).
- [ ] `MemoryArtifacts.put_png` → keeps bytes in a dict, exposed via `RunResult.steps[i].screenshot`.
- [ ] `Recorder` unchanged except importing the Protocols from the new modules.

### 2.4 Conformance test (the make-or-break gate)

- [ ] `tests/test_store_conformance.py` — run an identical scripted agent run (a stub
      provider that returns canned observations) against `MemoryStore` and `SqliteStore`;
      assert identical step trail + message history. Fails if any Store method is unimplemented.
- [ ] `tests/test_no_env.py` — `import agenticbrowser; BrowserAgent(keys={...}, enforce_byok=True)`
      with **no `.env` and no DATABASE_URL**; assert it constructs and never reads env keys.

**Acceptance:** `import agenticbrowser` works with no Postgres and no `.env`; the gateway
still boots unchanged via `Settings.to_core_config()`.

---

## 3. Cross-cutting workstreams

### W-A — Apache-2.0 licensing

- [ ] Add `LICENSE` (Apache-2.0 full text) + `NOTICE` (copyright + third-party attributions).
- [ ] `pyproject.toml`: `license = "Apache-2.0"`, `license-files = ["LICENSE"]`, add
      `classifiers` (License :: OSI Approved :: Apache Software License, Python 3.13, etc.).
- [ ] **Attribution audit:** README says action-vocabulary/stealth were *"adapted from the
      Computer-Use reference in `computers/`"*. Confirm that source's license is
      Apache/MIT-compatible and record it in `NOTICE`; if not, reimplement or get clearance.
- [ ] Per-file SPDX headers (`# SPDX-License-Identifier: Apache-2.0`) via a one-shot script.
- [ ] README license badge + `CONTRIBUTING.md` + DCO sign-off (lighter than a CLA).
- [ ] npm `packages/react/package.json`: `"license": "Apache-2.0"`.

### W-B — Freeze + version the StreamEvent contract

The `emit`/`StreamEvent` union (13 types, `models.py:136-144`) is the shared wire format
for SDK `stream()`, CLI `--ndjson`, MCP progress, webhooks, **and** the React embed.

- [ ] Move `StreamEvent` to `agenticbrowser/events.py`; add `EVENT_SCHEMA_VERSION = "1.0"`.
- [ ] **Type the `data` payloads** — today `data` is an untyped `Mapping`. Define a typed
      schema per event type (TypedDict or pydantic) for all 13:
      `token, thinking, tool_call, action, observation, approval_request, final, error,
      lease, live_view, subagent_start, subagent_end, interrupted` — plus the new
      `usage` event (W-C). Emit a machine-readable JSON Schema (`events.schema.json`).
- [ ] **Versioning policy:** additive field/event = minor; rename/remove = major. Consumers
      negotiate via `protocolVersion` returned from `/api/config` and from the SDK/CLI/MCP handshake.
- [ ] **Drift test** `tests/test_event_contract.py` — snapshot the event union + each `data`
      key set; fail CI when `runner.py`/`agent.py`/`events.py` add/rename/drop a field without
      a version bump. (Note: `lease`/`live_view` are emitted on the `/ws/view` plane as plain
      dicts, not chat-plane StreamEvents — document this split in the schema.)
- [ ] Codegen `events.d.ts` (TypeScript) from the JSON Schema for `packages/react` (Group 3).

### W-C — Cost observability

- [ ] `agenticbrowser/usage.py` — `UsageMeter` accumulating per turn: requests,
      input/output/total tokens (from PydanticAI `result.usage()`; note `agent.py:338,464`
      already threads `usage=ctx.usage` so sub-agent usage rolls up), step count (the `_idx`
      counter), and `browser_seconds` (session wall-clock). Add a per-model **price table**
      (`{model_spec: (in_per_mtok, out_per_mtok)}`) → estimated `cost_usd`.
- [ ] New **`usage` StreamEvent** emitted at turn end (and optional per-step deltas), added to
      the W-B contract. Surfaced on: SDK `RunResult.usage`, CLI JSON envelope `.usage`,
      MCP tool-result metadata, Docker webhook payload.
- [ ] **Budgets:** `CoreConfig.max_steps/max_tokens/max_cost_usd`. Enforce in `runner.run_turn`
      (check after each step / `AgentRunResultEvent`) → raise `BudgetExceeded`, which unwinds
      cleanly via the existing `CancelledError` path (releases leases, persists partial). CLI
      maps it to exit code 4.
- [ ] **Optional OpenTelemetry** (extra `[otel]`): a span per turn/step/tool with token/cost
      attributes; no-op when the dep is absent. PydanticAI already integrates with
      OTel/Logfire — wire it through rather than reinvent.

### W-D — Approval-gate v2

Today: a whole-word regex (`agent.py:_DESTRUCTIVE_RE` = `pay|buy|delete|send|confirm
order|transfer|checkout`) + `_SENSITIVE_RE` = `submit|login`, and `click_at` only gates when
the model sets a `label`. Gaps: coordinate clicks with no label, localized/novel verbs,
no spend ceiling, no URL scoping.

- [ ] `agenticbrowser/approval.py` — `ApprovalPolicy`:
  - expanded **multilingual / synonym** destructive verb set + a structured `RiskTaxonomy`
    (payment, deletion, messaging, auth, irreversible-submit);
  - **optional model-assisted classifier** — a cheap LLM call (reuse the tier menu) that
    classifies an action's risk when the regex is uncertain; cached; behind a flag so the
    fast path stays regex-only;
  - **URL allow/deny lists** + per-domain rules (e.g. always-gate on `*.bank.com`);
  - **spend ceiling** tied to W-C (`max_cost_usd` / per-action dollar cap) — gate any action
    once projected spend crosses the cap;
  - **fail-closed on unlabeled coordinate clicks**: a `click_at` with no `label` that lands on
    an interactive/button-like element is treated as SENSITIVE→requires label or denial,
    instead of slipping through.
- [ ] Refactor `agent.py::_classify`/`_gate` to delegate to `ApprovalPolicy` (keep the
      `ApprovalRequired` → `DeferredToolRequests` mechanism intact — it's the resume seam every
      form-factor reuses via `runner.submit_approval`).
- [ ] Document the gate's **coverage limits** prominently (it is a safety rail, not a
      guarantee) — required by the "safe enough to spend money" positioning.
- [ ] Tests: multilingual checkout buttons, unlabeled destructive coordinate click, spend-cap trip.

---

## 4. Group 1 — embeddable core

### 1a — Python SDK (`agenticbrowser/sdk.py`)

`BrowserAgent` async context manager over the Phase-0 core. Internally mints
session_id/chat_id UUIDs, builds a single-session `SessionRegistry(MemoryStore|SqliteStore, cfg)`,
a `Recorder(store, Null|MemoryArtifacts)`, and a `Runner`.

```python
class BrowserAgent:
    def __init__(self, *, keys, backend="local", browserbase=None, headless=True,
                 model=None, approve=None, subagents=False, max_concurrent_subagents=1,
                 max_tabs=6, enforce_byok=True, persist=None, artifacts=None,
                 max_steps=None, max_tokens=None, max_cost_usd=None): ...
    async def __aenter__(self) -> "BrowserAgent": ...      # opens browser session
    async def __aexit__(self, *exc): ...                    # closes + releases (Browserbase too)
    async def run(self, goal) -> RunResult: ...             # final output + steps + usage
    def stream(self, goal) -> AsyncIterator[StreamEvent]: ...
    async def steer(self, text): ...                        # maps to runner.start_turn (interrupts)
    def live_view(self) -> AsyncIterator[Frame]: ...        # screencast PNG or bb iframe url
    async def takeover(self): ...  async def handback(self): ...  async def send_input(self, evt): ...
    @property
    def messages(self): ...   async def export_messages(self): ...
```

- [ ] **emit→approve bridge:** translate the `approval_request` StreamEvent into a call to the
      user's `approve=` async callback, feed the result back via `runner.submit_approval`.
      Default `approve=None` → **auto-deny destructive** (fail safe).
- [ ] `live_view()`/`takeover()` reuse the registry per-tab lease (`acquire('human', ...)`) and
      the session screencast — the same plumbing `/ws/view` uses, handed to the caller.
- [ ] `RunResult` = `{output, steps:[{idx,action,url,ok,changed,screenshot?}], usage, approvals}`.
- [ ] **Playwright binary:** add `agenticbrowser install` console-script (wraps
      `playwright install chromium`) + a clear error if the browser is missing; advertise
      `backend="browserbase"` as the no-local-browser path.
- [ ] Public exports in `agenticbrowser/__init__.py`: `BrowserAgent, StreamEvent, Approval,
      ApprovalRequest, RunResult, Frame, Store, ArtifactStore, CoreConfig`.

### 1b — PydanticAI adapter (`agenticbrowser/adapters/pydantic_ai.py`)

Near-zero code: the tools in `agent.py` are already PydanticAI v2 functions on `AgentDeps`.

- [ ] `EphemeralBrowser` ctx-mgr (MemoryStore + Null/MemoryArtifacts + local-Chromium registry,
      no-op or user emit).
- [ ] `BrowserToolset(browser)` binds the 13 browsing tools to an `AgentDeps` so a consumer
      attaches it to their own `Agent`. Approval propagates **natively** as
      `DeferredToolRequests` → consumer resolves with `ToolApproved()/ToolDenied()`.
- [ ] Deferred (M2+, demand-driven): `adapters/langchain.py`, `crewai.py`, `llamaindex.py`,
      `temporal.py` — each <60 lines over a shared `run_browser_task(goal, *, session_id, model,
      byok_keys, on_approval, on_event)` helper.

### 1c — MCP server (`agenticbrowser/mcp/server.py`, extra `[mcp]`)

- [ ] FastMCP server. Primary tool `browse_task(goal, session_id?, model_alias="smart") -> str`
      (one autonomous call vs ~50 low-level round-trips). Opt-in `--expose-low-level` registers
      `navigate/act/screenshot/click_at/extract` mirroring `agent.py`.
- [ ] `open_session()/close_session()` for named, resumable (storage_state) sessions — solves
      the auth-wall problem (log in once via live-view, reuse).
- [ ] **Approval = MCP elicitation**: `ApprovalRequired`/`DeferredToolRequests` → elicitation
      request; host's callback approves/denies. No elicitation capability → auto-deny (safe).
- [ ] StreamEvents → MCP **progress notifications**; `usage` (W-C) on the tool result.
- [ ] Transports: stdio (Claude Desktop default) + Streamable-HTTP/SSE (remote). BYOK via
      launch-args env → `CoreConfig.server_keys` / `enforce_byok=True`.
- [ ] `console_scripts: browser-agent-mcp`. Optional `live_view_url` field for Browserbase.

### 1d — CLI (`agenticbrowser/cli.py`, Typer)

- [ ] Commands: `run <goal|->`, `session new|list|rm`, `replay <run-id>`, `install`.
- [ ] **Stable exit codes:** 0 done · 2 usage/config · 3 approval-required-and-denied ·
      4 budget/timeout (W-C) · 5 runtime error · 130 interrupted.
- [ ] **Output contract:** `--json` (one final envelope `{schema_version, run_id, status, result,
      steps, approvals, usage, error}`), `--ndjson` (one StreamEvent per line — literal emit
      serialization), default = pretty TTY.
- [ ] **Approval policy flags:** `--on-destructive deny|allow|prompt`, `--approve "submit,login"`,
      `--deny "pay,buy,..."` → drive `ApprovalPolicy` (W-D), answered via `runner.submit_approval`.
- [ ] `--watch` opens the live-view (screencast/iframe) for human takeover mid-run;
      `--session <name>` reuses a logged-in `SqliteStore` session; `--browserbase` flips provider.
- [ ] `pyproject [project.scripts] agenticbrowser = "agenticbrowser.cli:main"`.

---

## 5. Group 2 — self-host Docker (extra `[server]`; needs NO Phase 0)

Ships the full Postgres-backed gateway. Needs W-B (for typed clients) only.

- [ ] **Multi-stage `Dockerfile`** from `mcr.microsoft.com/playwright/python:v1.60.0`
      (Chromium + system libs baked): `uv sync` → `playwright install chromium` → COPY
      `agenticbrowser/` + `main.py` + pre-built `frontend/dist` (CI runs `npm run build`).
      `ENTRYPOINT fastapi run main:app`. `shm_size: 1gb` documented in compose.
- [ ] `:slim` tag (FROM python-slim, **no** Chromium) for `BROWSER_PROVIDER=browserbase`
      deployers (~700 MB smaller).
- [ ] `docker-compose.yml`: `browserbox` + optional bundled `postgres` (volume); document
      "use external managed Postgres for production". `SERVE_UI=true|false` gates the existing
      `app.frontend(...)` mount in `gateway.py` (already single-origin).
- [ ] New `server/settings.py` fields: `serve_ui`, `webhook_url`, `webhook_secret`.
- [ ] **Async run + webhook** (reuses the `emit` seam — no core change):
      `POST /api/chats/{id}/runs` → `{run_id}` (calls existing `runner.start_turn` with an emit
      that fans out to the WS queue **and** a `server/webhook.py` poster: HMAC-SHA256-signed
      JSON per StreamEvent, retry+backoff, DLQ); `GET /api/runs/{id}` poll fallback (the
      Postgres step-trail is the durable record).
- [ ] **OpenAPI codegen in CI** → `agenticbrowser` (PyPI) + `@agenticbrowser/client` (npm)
      typed clients, versioned to the image tag. Hand-author the WS framing + StreamEvent union
      (W-B schema) into the OpenAPI `components` (FastAPI auto-documents REST only); a
      schema-vs-runtime test guards drift.
- [ ] Volume for `artifacts_dir`; document **single-replica / sticky-session** (in-memory
      registry + leases don't transfer across replicas).

---

## 6. Group 3 — embeddable widget (`packages/react`; needs NO Phase 0)

Extract the reusable, already-presentational UI; sever the three repo couplings in `api.js`.

**The coupling to break (`frontend/src/api.js`):** `getToken()` reads
`localStorage['ab_token']` (lines 4-8); `wsUrl()` hardcodes `location.protocol`/`location.host`
(lines 68-72); same-origin REST in `req()` (line 12).

- [ ] Replace module-level token/host with an `AgentBrowserProvider` React context
      `{baseUrl, token, fetchToken?}`. `req()`, `wsUrl()`, `artifactUrl()` become context-bound
      (absolute cross-origin URLs, injected token + refresh). The existing `frontend/` wraps its
      tree in the provider seeded from localStorage + same-origin → keeps working.
- [ ] Move into `packages/react/src` (verbatim, only swap `../api.js` imports for context hooks):
      `chat/{ChatPanel,Composer,ApprovalModal,MiniChat,StepShot,TurnTrail,chatReducer,useChat}`
      + `live/LivePanel.jsx`. `chatReducer.js`/`useChat.js` are pure → the public state model.
      **Not** extracted: `App,Workspace,auth,Settings,NewSessionModal` (app-shell);
      `AuditView` optional behind `capabilities.stepTrail`.
- [ ] Theme: extract the CSS-custom-property contract (`--bg/--surface/--text/--accent/--agent/
      --human/--ok/--bad`, `[data-theme=light]`) + a `theme` prop → scoped CSS-var block.
- [ ] Public API:
      `<AgentBrowser baseUrl token chatId sessionId layout theme capabilities onEvent
      onApprovalRequest onTakeover/>`. `onEvent` re-exposes the **same StreamEvent objects**
      (W-B) the reducer consumes.
- [ ] **Web component** `<agentic-browser>` (Shadow DOM, Preact/compat to avoid double-React)
      for Vue/Svelte/Angular/plain HTML.
- [ ] **iframe embed**: `/embed` route in `gateway.py` (mirrors `app.frontend('/')`); token via
      `postMessage` (never in URL). `cors_origins` (config) documented as the consumer's origin.
- [ ] **Short-lived, chat-scoped tokens:** add a token-mint endpoint / `fetchToken` refresh —
      embeds must NOT carry the long-lived login token (`auth.py` issues long-lived today).
- [ ] Ship `events.d.ts` from W-B; freeze a `protocolVersion` handshake on `/api/config` so old
      bundles don't silently drop new events.
- [ ] Refactor `frontend/` to consume `@agenticbrowser/react` (single source of truth).

---

## 7. Testing & CI

- [ ] Phase-0: `test_store_conformance`, `test_no_env` (§2.4).
- [ ] W-B: `test_event_contract` drift guard + TS codegen check.
- [ ] W-C: budget-trip + usage-accounting tests.
- [ ] W-D: approval-classifier tests (multilingual, unlabeled coord click, spend cap).
- [ ] Adapter smoke: a real PydanticAI run with `BrowserToolset` against a fixture site.
- [ ] Group 2: `docker build` + boot + `/api/health` + one webhook delivery in CI.
- [ ] Group 3: build `packages/react`, render `<AgentBrowser>` against a mock WS.
- [ ] Reuse `evals.py` (`pydantic_evals`) as a regression gate on agent capability.

---

## 8. Risks & open decisions

- **Package rename touches deploy** (`main.py`, `pyproject [tool.fastapi]`, `.fastapicloud/`,
  `run.sh`) — do it at M0 before more files accrete; verify a FastAPI Cloud deploy after.
- **Playwright ↔ Chromium version skew** on the `pip` path — pin Playwright and document the
  matching `playwright install`; the Docker image avoids this by baking the matched pair.
- **Sandboxing (decided to flag, not solve here):** any headless embed runs a real browser
  visiting attacker pages next to app secrets. Recommend (and document) containerized Chromium /
  network-namespace for untrusted targets; default `enforce_byok` keeps server keys out.
- **Prompt-injection / SSRF:** add an egress allowlist hook at the session layer (every backend
  embed, not just the dropped SaaS) — track as a fast-follow to W-D.
- **Price table maintenance (W-C):** model prices drift; keep the table data-only and
  documented as best-effort estimates.
- **Open decision — adapter breadth:** PydanticAI at M1; LangChain/CrewAI/Temporal only as
  demand appears (each is churn-prone surface). Confirm priority order before M2.

---

## 9. Suggested first PR (M0 slice)

1. Add `LICENSE`/`NOTICE` + `pyproject` license metadata (W-A).
2. Package rename `app/` → `agenticbrowser/` (+ `server/` submodule) with deploy shim.
3. `CoreConfig` + thread the 14 `settings()` call sites; `Settings.to_core_config()`.
4. `Store` Protocol + `MemoryStore` + `SqliteStore`; `Null/MemoryArtifacts`.
5. Injectable `enforce_byok`/`server_keys` in `models_registry` (the BYOK correctness fix).
6. `events.py` + `EVENT_SCHEMA_VERSION` + `test_event_contract` (W-B foundation).
7. Conformance + no-env tests green.

That single PR makes the project genuinely embeddable and is the gate for everything after.
