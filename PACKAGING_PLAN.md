# Implementation Plan — OSS Packaging (Groups 1–3)

Companion to [PACKAGING.md](PACKAGING.md). Scope locked by the following decisions:

- **License: Apache-2.0** (permissive, maximize adoption).
- **No SaaS** — Group 4 (hosted multi-tenant) is dropped.
- **Ship Groups 1 & 2:** the embeddable core (SDK + adapters + MCP server) and the
  self-host Docker service. (No CLI and no embeddable frontend widget — out of scope; the
  existing `frontend/` covers the UI need.)
- Four cross-cutting must-haves: **freeze+version the StreamEvent contract**, **cost
  observability**, **improve the approval gate**, **Apache-2.0 licensing**.

Everything rides one decoupled core (Phase 0). The four cross-cutting workstreams
(W-A…W-D) land alongside Phase 0 because every form-factor depends on them.

---

## Status

**Shipped: SDK + PydanticAI adapter + MCP + Docker self-host** (branch
`packaging-m0`, uncommitted). 28 Python tests pass (incl. real-Chromium E2E); the
wheel builds and `docker compose config` validates.

- **M0:** imports as `agenticbrowser` with **no `.env`/Postgres**; keys come only from
  what's passed (no env fallback); event contract frozen + tested. (§9)
- **M1:** **`BrowserAgent` SDK** + **PydanticAI adapter** (`browse_task`) +
  `agenticbrowser-install`; runner emits a `usage` event + enforces `max_steps`/`max_tokens`.
  ([docs/sdk.md](docs/sdk.md); §10)
- **M2 (Group 1c):** **MCP server** — `browse_task` + session tools, approval-as-elicitation,
  `agenticbrowser-mcp`, `[mcp]` extra. ([docs/mcp.md](docs/mcp.md); §11)
- **M3 (Group 2):** **Docker self-host** — Dockerfile + compose, `SERVE_UI`, fire-and-forget
  **polling** runs (`POST /api/chats/{id}/runs`, `GET /api/runs/{id}`, `/stop`, `/approvals`).
  ([docs/self-host.md](docs/self-host.md); §11)

**Out of scope** (no concrete consumer to justify the maintenance surface): a **CLI** (the
SDK + MCP cover scripting/automation); a **React widget** (the existing `frontend/` already
covers "with a frontend"); and **webhook push** (the runs plane is polling-only). The
single-team self-host uses provider keys from its `.env`, so there is no per-session
key-entry surface; deploy is Docker / local self-host only.

**Deferred (not started):** W-C cost `$` price table + OTel; W-D approval-gate v2;
SDK `live_view()`/`takeover()`/`steer()`; the fine-grained 13-tool `BrowserToolset`.

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
        │        └──► 1c  MCP server          (FastMCP wrapper)
        │
        └──► Group 2   Docker self-host       (needs NO Phase 0)
```

| Milestone | Contents | Gate |
|---|---|---|
| **M0 ✅** | W-A, Phase 0, W-B + key-resolution fix | conformance test green; `import agenticbrowser` works with no `.env`/DB |
| **M1 ✅** | Group 1a SDK + 1b PydanticAI adapter; usage event + budgets | `await agent.run(...)` works headless (real-Chromium E2E green) |
| **M2 ✅** | Group 1c MCP server | `browse_task` + session tools + approval-as-elicitation; 7 tests |
| **M3 ✅** | Group 2 Docker self-host | Dockerfile + compose + polling `/runs` API (wheel builds, compose valid) |

---

## 1. Package & repo restructure (prereq for distribution)

The import name is currently `app` — unshippable. Target: one distribution
`agenticbrowser` with optional extras, core importable with zero server deps.

**Target layout:**

```
agenticbrowser/                 # installable CORE + SDK (no FastAPI, no Postgres)
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
  server/                       # the GATEWAY (moved from app/, server-only)
    gateway.py auth.py settings.py store_sql.py
frontend/                       # unchanged app
LICENSE NOTICE                  # W-A
```

**Migration tasks**

- [ ] `git mv app/{agent,runner,registry,session,providers,recorder,models,models_registry,history}.py agenticbrowser/`
- [ ] `git mv app/gateway.py app/auth.py agenticbrowser/server/`; rename
      `app/store.py` → `agenticbrowser/server/store_sql.py` (the SQLModel/Postgres impl,
      keeps **all** user/auth/chat methods).
- [ ] Split `app/config.py`: the `Settings` (pydantic-settings) class →
      `agenticbrowser/server/settings.py`; the new `CoreConfig` → `agenticbrowser/config.py`.
- [ ] Fix imports (`.config import settings` → see Phase 0); keep relative imports inside the package.
- [ ] **Deploy shim:** `main.py` becomes `from agenticbrowser.server.gateway import app`;
      `run.sh` uvicorn target → `agenticbrowser.server.gateway:app`.
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
    # models / provider keys
    agent_model: str = "anthropic:claude-sonnet-4-6"   # models_registry:85,99
    provider_keys: dict[str, str | None] = field(default_factory=dict)  # {anthropic,openai,google}
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
- [ ] `providers.py` — `make_provider(name, *, cfg)`;
      `LocalProvider(cfg)` uses `cfg.headless` (:82); `BrowserbaseProvider(cfg)`
      uses `cfg.browserbase_*` (:112); :195 default from `cfg`.
- [ ] `session.py` — pass screencast params (`cfg`) into `PlaywrightSession`; replace :393, :408.
- [ ] `models_registry.py` — make `provider_keys` injectable. Functions
      `_pick_key`, `available_providers`, `pick_model`, `build_model`,
      `build_vision_model` take a `cfg: CoreConfig`.
      **This is the single most important fix:** today (:67,:76,:113) it can resolve a stray
      key from `os.environ`. After this, keys come ONLY from `cfg.provider_keys` (no
      per-session key, no ambient-env fallback) — an embed with only its own keys can never
      touch a stray env key.
- [ ] `server/gateway.py` lifespan — build `cfg = settings().to_core_config()` once; pass
      to `SessionRegistry(store, cfg)`, `Recorder(store, artifacts)`, `Runner(...)`. Add
      `Settings.to_core_config()` in `server/settings.py` (the one producer).
- [ ] Leave gateway-only `settings()` users untouched: `auth.py:51` (token_ttl),
      `crypto.py:29` (app_secret), `gateway.py` artifacts_dir/cors/registration.

### 2.2 `Store` Protocol + impls — `agenticbrowser/stores.py`

The core calls **exactly 11 methods** (grep-verified across registry/runner/recorder).
Keys are NOT a Store concern — they live in `CoreConfig.provider_keys` (and browserbase
creds in `CoreConfig.browserbase_*`), never the store:

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
    # runner.py
    async def load_messages(self, chat_id) -> list: ...
    async def save_messages(self, chat_id, messages) -> None: ...
    async def max_step_idx(self, chat_id) -> int: ...
    # recorder.py
    async def insert_step(self, rec: StepRecord) -> None: ...
```

- [ ] Define the Protocol; the existing SQL class (`server/store_sql.py`) already satisfies
      it (plus its server-only user/auth/chat methods) — add `Store` to its bases for clarity.
- [ ] `MemoryStore` — dicts in RAM. Messages + steps held in lists. No key/cred state.
- [ ] `SqliteStore(path)` — `aiosqlite`, **WAL mode** (sub-agents write steps concurrently).
      Persists storage_state, bb_session_id, last_url, messages-blob, steps. No key/cred state.
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
- [ ] `tests/test_no_env.py` — `import agenticbrowser; BrowserAgent(keys={...})` with
      **no `.env` and no DATABASE_URL**; assert it constructs and never reads env keys.

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

### W-B — Freeze + version the StreamEvent contract

The `emit`/`StreamEvent` union is the shared wire format for SDK `stream()`, MCP
progress, **and** the gateway's chat WS / polling runs.

- [ ] Move `StreamEvent` to `agenticbrowser/events.py`; add `EVENT_SCHEMA_VERSION = "1.0"`.
- [ ] **Type the `data` payloads** — today `data` is an untyped `Mapping`. Define a typed
      schema per event type (TypedDict or pydantic) for all 13:
      `token, thinking, tool_call, action, observation, approval_request, final, error,
      lease, live_view, subagent_start, subagent_end, interrupted` — plus the new
      `usage` event (W-C). Emit a machine-readable JSON Schema (`events.schema.json`).
- [ ] **Versioning policy:** additive field/event = minor; rename/remove = major. Consumers
      negotiate via `protocolVersion` returned from `/api/config` and from the SDK/MCP handshake.
- [ ] **Drift test** `tests/test_event_contract.py` — snapshot the event union + each `data`
      key set; fail CI when `runner.py`/`agent.py`/`events.py` add/rename/drop a field without
      a version bump. (Note: `lease`/`live_view` are emitted on the `/ws/view` plane as plain
      dicts, not chat-plane StreamEvents — document this split in the schema.)

### W-C — Cost observability

- [ ] `agenticbrowser/usage.py` — `UsageMeter` accumulating per turn: requests,
      input/output/total tokens (from PydanticAI `result.usage()`; note `agent.py:338,464`
      already threads `usage=ctx.usage` so sub-agent usage rolls up), step count (the `_idx`
      counter), and `browser_seconds` (session wall-clock). Add a per-model **price table**
      (`{model_spec: (in_per_mtok, out_per_mtok)}`) → estimated `cost_usd`.
- [ ] New **`usage` StreamEvent** emitted at turn end (and optional per-step deltas), added to
      the W-B contract. Surfaced on: SDK `RunResult.usage`, MCP tool-result metadata, and the
      Docker run-poll payload. *(M1: event + token counts landed; `cost_usd` pending the price table.)*
- [ ] **Budgets:** `CoreConfig.max_steps/max_tokens/max_cost_usd`. Enforce in `runner.run_turn`
      via `UsageLimits`. *(M1: max_steps/max_tokens landed; max_cost_usd pending the price table.)*
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
                 max_tabs=6, persist=None, artifacts=None,
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
- [ ] Transports: stdio (Claude Desktop default) + Streamable-HTTP/SSE (remote). Provider
      keys via launch-args env → passed as the session's keys (no ambient-env fallback).
- [ ] `console_scripts: agenticbrowser-mcp`. Optional `live_view_url` field for Browserbase.

> **CLI (former 1d) — removed.** A standalone TTY/JSON CLI is explicitly out of scope.
> The SDK + MCP server cover scripting/automation needs.

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
- [ ] New `server/settings.py` field: `serve_ui`.
- [ ] **Async run (polling)** (reuses the `emit` seam — no core change):
      `POST /api/chats/{id}/runs` → `{run_id}` (calls existing `runner.start_turn` with an emit
      that fans out to the WS queue and buffers events for poll fallback); `GET /api/runs/{id}`
      poll (the Postgres step-trail is the durable record). Polling-only; no webhook push.
- [ ] **OpenAPI codegen in CI** → `agenticbrowser` typed client, versioned to the image tag.
      Hand-author the WS framing + StreamEvent union (W-B schema) into the OpenAPI `components`
      (FastAPI auto-documents REST only); a schema-vs-runtime test guards drift.
- [ ] Volume for `artifacts_dir`; document **single-replica / sticky-session** (in-memory
      registry + leases don't transfer across replicas).

---

## 6. Embeddable widget — out of scope

A standalone, embeddable React widget / web component / `<iframe>` embed (`@agenticbrowser/react`,
an `/embed` route, a `protocolVersion` handshake) is **not** in scope. The existing `frontend/`
already covers the "ship a UI" need, and no concrete consumer justifies extracting and
maintaining a separate, cross-origin-embeddable package. If demand appears, the seam is the
frozen `StreamEvent` contract (W-B) plus decoupling the three `frontend/src/api.js` couplings
(localStorage token, hardcoded host in `wsUrl()`, same-origin REST in `req()`).

---

## 7. Testing & CI

- [ ] Phase-0: `test_store_conformance`, `test_no_env` (§2.4).
- [ ] W-B: `test_event_contract` drift guard.
- [ ] W-C: budget-trip + usage-accounting tests.
- [ ] W-D: approval-classifier tests (multilingual, unlabeled coord click, spend cap).
- [ ] Adapter smoke: a real PydanticAI run with `BrowserToolset` against a fixture site.
- [ ] Group 2: `docker build` + boot + `/api/health` + a `/runs` poll round-trip in CI.
- [ ] Reuse `evals.py` (`pydantic_evals`) as a regression gate on agent capability.

---

## 8. Risks & open decisions

- **Deploy** is **Docker / local self-host only**; the package rename touched `main.py` +
  `run.sh`, handled at M0. `main.py` stays a generic `main:app` ASGI shim.
- **Playwright ↔ Chromium version skew** on the `uv add` path — pin Playwright and document the
  matching `playwright install`; the Docker image avoids this by baking the matched pair.
- **Sandboxing (decided to flag, not solve here):** any headless embed runs a real browser
  visiting attacker pages next to app secrets. Recommend (and document) containerized Chromium /
  network-namespace for untrusted targets; the SDK uses only the keys you pass it (no
  ambient-env fallback), so a stray host env key stays out of the agent.
- **Prompt-injection / SSRF:** add an egress allowlist hook at the session layer (every backend
  embed, not just the dropped SaaS) — track as a fast-follow to W-D.
- **Price table maintenance (W-C):** model prices drift; keep the table data-only and
  documented as best-effort estimates.
- **Open decision — adapter breadth:** PydanticAI at M1; LangChain/CrewAI/Temporal only as
  demand appears (each is churn-prone surface). Confirm priority order before M2.

---

## 9. First PR (M0 slice) — ✅ DONE

- [x] Add `LICENSE` (Apache-2.0) + `NOTICE` + `pyproject` license metadata, classifiers,
      and optional-dependency extras `[server] [evals] [otel] [dev]` (W-A).
- [x] Package rename `app/` → `agenticbrowser/` (+ `server/` submodule) via `git mv`
      (history preserved); deploy shim updated (`main.py`, `run.sh`).
- [x] `CoreConfig` (`agenticbrowser/config.py`) + threaded all 14 `settings()` call sites
      in agent/providers/registry/session/models_registry; `Settings.to_core_config()` in
      `server/settings.py` is the one producer for the gateway.
- [x] `Store` Protocol + `MemoryStore` + `SqliteStore(WAL)` (`agenticbrowser/stores.py`);
      `ArtifactStore` + `Local/Null/MemoryArtifacts` (`agenticbrowser/artifacts.py`).
- [x] Injectable `provider_keys` in `models_registry` — the key-resolution correctness fix
      (keys come ONLY from `cfg.provider_keys`; no per-session key, no ambient `os.environ`
      fallback, so a stray env key can never leak in).
- [x] `events.py` + `EVENT_SCHEMA_VERSION="1.0"` + typed `EVENT_DATA_KEYS`; `StreamEvent`
      re-exported from `models.py` for back-compat.
- [x] Tests green: `tests/test_store_conformance.py` (Memory vs SQLite identical),
      `tests/test_no_env_keys.py`, `tests/test_event_contract.py`.

### Implementation notes / deviations from the original plan

- **SQL store class kept named `Store`** (in `server/store_sql.py`), not `SqlStore` —
  the Protocol is `Store` in `stores.py`; the concrete SQL class is `Store` in
  `server/store_sql.py`. They live in different modules and never collide, so
  gateway/auth/evals keep `from .store_sql import Store` with zero annotation churn.
  (Lower-risk than the planned rename; revisit only if it confuses contributors.)
- **No `[build-system]` block yet** — dev runs via `PYTHONPATH` (unchanged). Real
  installability (build backend + wheel) is deferred to M1 with the SDK.
- **Import-time prompt fix:** `ORCHESTRATOR_PROMPT` no longer interpolates
  `settings()`; an `@agent.system_prompt` dynamic function injects the concrete
  sub-agent/tab limits from `ctx.deps.cfg` per run.
- **`usage` event forward-declared** in the W-B contract now (type + data keys) so the
  schema is stable before W-C actually emits it.
- **Server env-key push retained** in `server/settings.py.settings()` (os.environ
  setdefault) so PydanticAI's env-based resolution finds the server's `.env` keys; this is
  server-only — the core never reads `os.environ`.
- **Conformance test scope:** exercises the 11-method Store surface directly on both
  impls (fast, no browser). A full stub-provider end-to-end agent run is a later add.
- `server/evals.py` threaded with `CoreConfig()` to keep it importable/correct (dev-only).

This makes the project genuinely embeddable and is the gate for everything after.

---

## 10. M1 — SDK + PydanticAI adapter — ✅ DONE

- [x] **`BrowserAgent` SDK** (`agenticbrowser/sdk.py`) — async context manager over the
      existing `Runner`. `run(goal) -> RunResult`, `stream(goal)` (async iterator),
      `approve=` callback (auto-deny default), `persist=` (MemoryStore /
      `sqlite:///path` / custom Store), `subagents=`, `max_steps/max_tokens`,
      `backend="local"|"browserbase"`, `export_messages()`. Internally mints
      session/chat ids, builds MemoryStore/SqliteStore + `MemoryArtifacts` + a
      single-session registry, and drives `Runner.run_turn`.
- [x] **Approval bridge** — `approval_request` events are resolved via the user's
      `approve=` handler in a deferred task (so `Runner._collect`'s future is
      registered first) and fed back through `runner.submit_approval`. Returns
      `Approval` / `True` / `str`; omitted ⇒ auto-deny (fail-safe).
- [x] **Usage event + budgets (W-C foundation)** — `runner.py` accumulates
      `result.usage()` across a turn's segments, emits a `usage` StreamEvent
      (`steps/requests/input_tokens/output_tokens/total_tokens/cost_usd`), and
      applies `UsageLimits(request_limit=None, tool_calls_limit=max_steps,
      total_tokens_limit=max_tokens)`; `UsageLimitExceeded` → clean "Budget exceeded".
- [x] **PydanticAI adapter** (`agenticbrowser/adapters/pydantic_ai.py`) —
      `EphemeralBrowser` (long-lived session) + `as_tool()` and `browse_task_tool()`
      exposing a `browse_task(goal)` tool for any PydanticAI `Agent`.
- [x] **`agenticbrowser-install`** console-script (`agenticbrowser/install.py`) +
      `[project.scripts]`; the SDK raises a clear "run install / use browserbase"
      error if Chromium is missing.
- [x] **Exports** updated in `agenticbrowser/__init__.py` (BrowserAgent, RunResult,
      Approval, ApprovalRequest).
- [x] **Tests** (19/19): `tests/test_sdk.py` (config/store/approval units + two
      **real-Chromium** E2E: navigate→finish, destructive-click→auto-deny→resume),
      `tests/test_adapter_pydantic_ai.py`. E2E uses a scripted `FunctionModel`
      `stream_function` (no LLM key/network) via `agent.override(model=...)`.
- [x] **Docs** — [docs/sdk.md](docs/sdk.md) quickstart.

### Deviations from the original §4 plan

- **PydanticAI adapter ships the high-level `browse_task` tool, not the fine-grained
  13-tool `BrowserToolset`.** The low-level tools are bound to `RunContext[AgentDeps]`
  (and `ctx.usage`); exposing them in a *host's* run needs RunContext bridging so
  approvals propagate as native `DeferredToolRequests`. Deferred — `browse_task`
  (one autonomous call, approvals handled inside via `approve=`) is the robust M1
  shape and is also exactly what the M2 MCP server will reuse.
- **`live_view()` / `takeover()` / `send_input()` and mid-run `steer()` are deferred.**
  Multi-turn already works by calling `run()`/`stream()` again (history accumulates
  under one chat id). The interactive human-takeover surface lands in a later piece.
- **Cost in `$` is not yet computed** — the `usage` event carries token counts;
  `cost_usd` is `None` until the W-C price table + OTel spans land. `max_cost_usd` is
  accepted but not yet enforced (token/step budgets are).
- **W-D (approval-gate v2) not started** — the SDK exposes the *existing* regex gate
  via `approve=`. Improving the classifier (multilingual verbs, unlabeled-click
  fail-closed, URL allow/deny, spend ceiling) is the next focused piece.

---

## 11. M2–M3 — MCP + Docker — ✅ DONE

### M2 — MCP server (Group 1c)
- [x] `agenticbrowser/mcp/server.py` (FastMCP). Tools: `browse_task(goal, session_id="")`,
      `open_session`, `close_session`, `list_sessions`. Reuses the `BrowserAgent` SDK;
      persistent sessions kept in a module dict for cross-call reuse.
- [x] **Approval = MCP elicitation** (`_make_approver`): asks the host via `ctx.elicit`;
      falls back to auto-deny (or `BROWSER_AGENT_AUTO_APPROVE=true`) when the host can't.
- [x] provider keys + config via env; transports `stdio`/`sse`/`streamable-http`
      (`BROWSER_AGENT_MCP_TRANSPORT`). `[mcp]` extra + `agenticbrowser-mcp` console script.
- [x] 7 tests (`tests/test_mcp.py`): tool registration + the elicitation bridge. Docs:
      [docs/mcp.md](docs/mcp.md).
- Deviation: low-level tools (`--expose-low-level`) and Browserbase `live_view_url` are
  deferred; `browse_task` (one autonomous call) is the shape shipped.

### M3 — Docker self-host (Group 2)
- [x] Multi-stage **`infra/Dockerfile`** (node build of `frontend/` → Playwright python
      runtime, runs from `/app` source so `ROOT=/app`) + **`.dockerignore`** (at the repo
      root, the build-context root).
- [x] **`infra/docker-compose.yml`**: `app` + healthchecked `postgres`; `shm_size: 1gb`;
      env-driven (`SERVE_UI`, provider keys, bootstrap, …); `build.context: ..` so COPY
      paths stay repo-root-relative. `infra/run.sh` starts only the `postgres` service for
      local dev.
- [x] **`infra/` + root `Makefile`**: all Docker/bash ops live under `infra/`; the
      `Makefile` wraps them (`make up`/`run`/`dev`/`down`/`db`/`config`).
- [x] `server/settings.py`: `serve_ui`; the UI mount is gated on it.
- [x] **Fire-and-forget runs (polling)** (no core change — rides `emit`): `POST
      /api/chats/{id}/runs` → `{run_id}`, `GET /api/runs/{id}`, `POST
      /api/runs/{id}/approvals`, `POST /api/runs/{id}/stop`. (Polling-only; no webhook push.)
- [x] **`[build-system]` (hatchling)** added — the package builds a wheel (verified), which
      the Docker `uv pip install ".[server,mcp]"` step uses, and enables installing from
      GitHub (`uv add "git+https://github.com/CharbelDaher34/agentic-browser.git"`).
- [x] Verified: wheel builds, `docker compose -f infra/docker-compose.yml config` valid,
      gateway exposes the run routes.
      Docs: [docs/self-host.md](docs/self-host.md).
- Deviation: the **image itself was not built here** (heavy Playwright base); the
  Dockerfile is verified by the wheel build + compose validation.
