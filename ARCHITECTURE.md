# Agentic Browser — architecture & design

An AI agent that drives a real web browser (local Playwright **or** Browserbase) to
accomplish goals described in plain language. It ships in four form-factors over **one
decoupled core**: an embeddable **Python SDK**, a **PydanticAI tool**, an **MCP server**,
and a self-hosted **FastAPI service** (REST + WebSockets + a bundled React UI).

This document describes how the code is structured and how the pieces fit together. For
*usage*, see [README.md](README.md) and [docs/](docs/) (`sdk.md`, `mcp.md`, `self-host.md`).

- Built on **PydanticAI v2** (`pydantic-ai-slim==2.0.0`) + **Playwright**.
- **Hybrid acting** — DOM mode (elements by `ref`) *and* vision mode (screenshot → click
  by pixel coordinates), so it handles canvas/maps/visual widgets a DOM-only agent misses.
- **Approval gate** — destructive actions (pay/buy/delete/send/checkout) pause for a
  decision; fail-safe (auto-deny unless allowed).
- **Human takeover** — a per-tab driver *lease* lets a human grab the wheel mid-run.
- **Parallel sub-agents** — an orchestrator delegates independent sub-tasks, each on its
  own browser tab.

---

## The one idea: a decoupled core + thin form-factors

The hard part — perceive → act → approve → stream → persist — lives in a dependency-light
**core** that runs headless with no FastAPI and no Postgres. Every form-factor is a thin
shell over that core:

```
agenticbrowser/                 # the CORE (importable with no server deps)
  config.py        CoreConfig — the injected runtime config (replaces a settings() global)
  models.py        value types: Action/ActionKind/Risk, PageObservation, StepRecord, Lease
  events.py        StreamEvent + the frozen, versioned wire contract (EVENT_SCHEMA_VERSION)
  providers.py     local Playwright (stealth CDP) vs Browserbase — one interface
  session.py       PlaywrightSession — multi-tab page wrapper: perceive, act, screencast
  registry.py      long-lived sessions + per-tab driver lease (human takeover) + reaping
  agent.py         the PydanticAI agents (orchestrator + sub-agents) and their tools
  runner.py        one streamed turn: events → StreamEvents, approvals, usage, persistence
  models_registry.py  provider-key resolution + Model construction (build/resolve_model)
  stores.py        Store Protocol + MemoryStore + SqliteStore (no DB required)
  artifacts.py     ArtifactStore Protocol + Local / Null / Memory screenshot sinks
  recorder.py      per-step screenshot + step row (the replay trail)
  history.py       message-history healing (dangling tool_use repair)
  sdk.py           BrowserAgent — the embeddable async API
  install.py       `agenticbrowser-install` — fetch the local Chromium
  adapters/
    pydantic_ai.py EphemeralBrowser + browse_task tool for any PydanticAI agent
  mcp/
    server.py      FastMCP server: browse_task + session tools (Claude Desktop / Cursor)
  server/                       # the SELF-HOST GATEWAY (extra: [server])
    gateway.py     FastAPI: auth, REST, chat WS, live-view/takeover WS, static UI
    store_sql.py   SQLModel/Postgres Store (users, auth, sessions, chats, messages, steps)
    settings.py    pydantic-settings Settings → Settings.to_core_config()
    auth.py        token auth (bcrypt + bearer/?token=)
    evals.py       pydantic_evals dataset + success evaluators (dev-only)
frontend/                       # React (Vite) chat UI, served single-origin by the gateway
infra/                          # Dockerfile, docker-compose.yml, run.sh
```

**Why it matters.** `import agenticbrowser` reaches no database and reads no `.env`. The
SDK constructs a `CoreConfig` from its args; the server builds one via
`Settings.to_core_config()`. The two never blur: the core takes config by injection.

---

## Install / run

Not on PyPI yet — install from GitHub with **uv** (see [README](README.md)):

```bash
# SDK / adapter
uv add "git+https://github.com/CharbelDaher34/agentic-browser.git"
# MCP extra
uv add "agenticbrowser[mcp] @ git+https://github.com/CharbelDaher34/agentic-browser.git"
uv run python -m agenticbrowser.install        # one-time: fetch local Chromium

# self-host (full service) — Docker files live in infra/, wrapped by the root Makefile
make up        # docker compose -f infra/docker-compose.yml up --build  → app on :8000
make run       # local dev (uvicorn + Postgres) in a detached `agenticemirates` screen
```

Base deps are `pydantic-ai-slim[anthropic,google,openai]==2.0.0`, `playwright`,
`browserbase`, `aiosqlite`. FastAPI/uvicorn/sqlmodel/asyncpg are the optional `[server]`
extra; `mcp` is `[mcp]`.

---

## `config.py` — `CoreConfig`

A frozen dataclass that is the entire runtime-config surface of the core. Threaded through
`AgentDeps`, the registry, providers, and the model registry so nothing in the core calls a
global or reads `os.environ`.

```python
@dataclass(frozen=True)
class CoreConfig:
    browser_provider: str = "local"            # "local" | "browserbase"
    headless: bool = True
    browserbase_api_key: str | None = None     # the Browserbase creds this process uses
    browserbase_project_id: str | None = None
    agent_model: str = "anthropic:claude-sonnet-4-6"   # orchestrator
    worker_model: str | None = None            # sub-agents; None -> agent_model
    provider_keys: dict[str, str | None] = {}  # the ONLY source of API keys (see below)
    max_subagent_depth: int = 1
    max_concurrent_subagents: int = 1
    max_tabs: int = 6
    idle_ttl_seconds: int = 1800
    screencast_quality: int = 60
    screencast_every_nth_frame: int = 1
    screencast_max_width / max_height: int | None = None
    max_steps / max_tokens: int | None = None  # budgets
    max_cost_usd: float | None = None
```

**Keys.** `provider_keys` is the **only** place keys come from — the SDK fills it from its
`keys=` arg, the server from its `.env` (via `Settings.to_core_config()`). The core never
reads `os.environ`, so a stray ambient key is never silently used. There is **no** per-session
key entry and **no** `enforce_byok` flag (both were SaaS-era; removed).

---

## `models.py` — value types

- **`ActionKind`** — 13 kinds. DOM: `navigate, click, type, select, scroll, extract`.
  Vision/coordinate: `click_at, type_at, scroll_at, drag, key, back, forward, wait`.
- **`Action`** — `kind`, `risk` (`Risk.SAFE|SENSITIVE|DESTRUCTIVE`), plus DOM fields (`ref`,
  `text`, `url`, `submit`) and vision fields (`x, y, x2, y2, keys, direction, magnitude,
  seconds, clear`). `to_json()` serializes for the step trail.
- **`PageObservation`** — `url, title, elements: Sequence[Element], text_digest,
  fingerprint`. `Element` is a frozen slots dataclass (`ref, role, name, value, enabled`),
  so `to_json()` builds the dict field-by-field (no `__dict__`).
- **`ActionResult`** — `ok, changed, observation, error?`.
- **`StepRecord`** — one row of the replay trail (chat/session/idx/action/result/screenshot).
- **`Lease`** — `driver: DriverKind`, `holder_id` (the per-tab takeover lease).
- `StreamEvent` is re-exported here from `events.py` for back-compat; it lives in `events.py`.

---

## `events.py` — the StreamEvent wire contract

One event type carries the whole streaming seam — SDK `stream()`, the gateway chat WS, and
MCP progress all emit `StreamEvent`. The contract is frozen and versioned
(`EVENT_SCHEMA_VERSION = "1.0"`), with `EVENT_DATA_KEYS` snapshot-tested for drift
(`tests/test_event_contract.py`).

Chat-plane event types: `token, thinking, tool_call, action, observation, approval_request,
final, error, interrupted, subagent_start, subagent_end, usage`.
View-plane (emitted on `/ws/view` as plain control messages, **not** chat StreamEvents):
`lease, live_view`.

`data` payload keys (per `EVENT_DATA_KEYS`):

| event | keys |
|---|---|
| `token` / `thinking` | `text` |
| `tool_call` | `tool, args` |
| `action` | `action, ref, agent, tab` |
| `observation` | `idx, url, title, ok, changed, agent, tab` |
| `approval_request` | `calls` |
| `final` / `interrupted` | `text` |
| `error` | `msg` |
| `subagent_start` | `id, task, model, tab` |
| `subagent_end` | `id, result, ok` |
| `usage` | `steps, requests, input_tokens, output_tokens, total_tokens, cost_usd` |

---

## `providers.py` — local vs Browserbase

`make_provider(name=None, *, cfg: CoreConfig)` returns a `BrowserProvider` reading
`cfg.browser_provider`. `provider.open(storage_state=None, *, reconnect_id=None)` returns an
**`OpenBrowser`** — `page, context, provider, live_view_mode, provider_session_id, cdp,
live_view_url, close, release`.

- **`LocalProvider`** — launches Chromium with stealth args + UA, restores `storage_state`
  (cookies + localStorage), and exposes a CDP session for screencast + input injection.
  `headless` comes from `cfg.headless`.
- **`BrowserbaseProvider`** — creds from `cfg.browserbase_*`. Supports `reconnect_id` to
  re-attach to a still-live keep-alive session (falls back to a fresh one if it's gone).
  `live_view_url` is the managed iframe with built-in takeover.

**Two teardown verbs:** `close()` only detaches the local connection; **`release()`** sends
Browserbase `REQUEST_RELEASE` to destroy the remote session (the cost-cap path).

---

## `session.py` — `PlaywrightSession` (multi-tab)

A provider-agnostic, **multi-tab** page wrapper. One browser context holds one or more
`_Tab`s (each with its own action lock, CDP, streaming state, subscriber set). The first tab
is primary (`t0`); public methods default to it. Opened via the classmethod
`PlaywrightSession.open(provider, storage_state, *, cfg, reconnect_id)`, which also installs
a popup-adoption listener so child windows become managed tabs.

- **Perceive:** `observe()` runs `_COLLECT_JS` in a retry loop (handles context-destroyed
  during navigation) → `PageObservation`.
- **Act/verify:** `_perform()` handles all 13 `ActionKind`s — DOM (by `ref`) and
  coordinate-based (vision) — then re-observes to report `changed`.
- **Tabs:** `open_tab / list_tabs / close_tab / has_tab`, plus `_on_popup`.
- **Live view + takeover:** per-tab screencast (`Page.screencastFrame`, quality/frame-skip
  from `CoreConfig`), `frame_jpeg_b64()`, and input injection — `inject_key()` (maps printable
  vs virtual keys via `_VK_CODES` for correct CDP dispatch) and `inject_scroll()`.
- **Teardown:** `release()` destroys a Browserbase session (else just closes locally).

---

## `stores.py` — the Store Protocol + zero-server impls

The core touches **exactly 11 Store methods** (grep-verified across registry/runner/recorder).
Keys are *not* a Store concern — they live in `CoreConfig`.

```python
class Store(Protocol):
    # browser/session state (registry)
    load_storage_state / save_storage_state / load_last_url
    load_bb_session_id / save_bb_session_id          # reconnect a live Browserbase session
    upsert_session / get_session
    # conversation (runner)
    load_messages / save_messages / max_step_idx
    # replay trail (recorder)
    insert_step
```

- **`MemoryStore`** — RAM only; ephemeral. For SDK/evals.
- **`SqliteStore(path)`** — `aiosqlite`, WAL mode; single-process resumable sessions + replay
  trail with no Postgres.
- The server's `server/store_sql.py` (SQLModel/Postgres) **also** satisfies this Protocol and
  adds server-only tables/methods (users, auth_sessions, chats ownership, message export). Six
  tables total: `users, auth_sessions, sessions, chats, messages, steps`.

A conformance test (`tests/test_store_conformance.py`) asserts MemoryStore ≡ SqliteStore
across the surface.

---

## `artifacts.py` + `recorder.py` — the replay trail

`ArtifactStore` Protocol (`put_png(key, bytes) -> uri`) with three impls:
`LocalArtifacts` (writes under `artifacts/`, returns an ownership-checked
`/api/artifacts/...` web path), `NullArtifacts` (drops bytes, returns a synthetic key —
headless), `MemoryArtifacts` (keeps bytes in RAM — embeds/evals).

`Recorder.record(...)` puts the screenshot then writes the `StepRecord` via `store.insert_step`.

---

## `registry.py` — `SessionRegistry`

`SessionRegistry(store, cfg)` owns long-lived sessions and the takeover leases.

- `create(session_id, provider)` opens the browser, restores `storage_state` + reconnects a
  prior Browserbase session (`load_bb_session_id`), and persists the (possibly new)
  `provider_session_id`.
- `ensure(session_id)` rehydrates a session from the store on demand (so a turn works after a
  process restart); `is_live`, `get`, `attach`, `detach`.
- **Per-tab leases:** `acquire / release / holds / wait_until_agent_may_drive` all take a
  `tab_id` (default `t0`). A human takeover pauses the agent on that tab and hands back on
  release — the single-driver guarantee that makes 2FA/CAPTCHA handoff safe.
- `reap_idle()` (TTL) persists `storage_state` + clears the bb-session id + `release()`s the
  remote browser; `shutdown()` saves + closes all on graceful stop.

---

## `agent.py` — orchestrator + sub-agents, DOM + vision

Two PydanticAI `Agent`s share one tool set:

- **`agent`** (orchestrator) — `output_type=[str, DeferredToolRequests]`; tools =
  `_ORCHESTRATOR_TOOLS` (the browsing tools **+** `finish` + the orchestration tools).
- **`subagent`** (worker) — `output_type=str`; tools = `_BROWSING_TOOLS` only. Cannot spawn
  further sub-agents and finishes by replying.

Neither hardcodes a model (`model=None`); the runner passes one per run via
`resolve_model(spec, cfg)`, so the provider follows `CoreConfig.provider_keys`.

**Browsing tools** (`_BROWSING_TOOLS`): `navigate, act, extract` (DOM) and `screenshot,
click_at, type_at, scroll, drag, press_key, go_back, go_forward, wait, locate` (vision).
`locate` calls a Gemini robotics-ER grounding model (`build_vision_model(cfg)`) to turn a
described element into pixel coordinates.
**Orchestrator-only tools:** `finish`, `spawn_subagents`, `spawn_subagent`, `open_tab`,
`list_tabs`, `close_tab`.

**`AgentDeps`** carries `session_id, chat_id, lease_token, registry, recorder, emit, _idx,
tab_id="t0", depth, label, cfg`.

**Approval gate.** `_classify(kind, ref, text)` uses whole-word regexes
(`_DESTRUCTIVE_RE` = pay/buy/delete/send/confirm order/transfer/checkout; `_SENSITIVE_RE` =
submit/login) so "paypal"/"sending" don't false-trip. `_gate()` returns `None` (proceed) or a
message; in the orchestrator a destructive action surfaces as `ApprovalRequired` →
`DeferredToolRequests` (the resume seam reused by every form-factor); sub-agents get a deny
message (they can't show approval UI).

---

## `runner.py` — one streamed turn

`Runner(registry, store, recorder, cfg)`. Entry points: `start_turn()` (launches a turn as a
task, **interrupting** any in-flight one first — mid-run steering), `stop()`, `run_turn()`
(the core loop), `submit_approval()`, `cancel_pending()`.

`run_turn` resolves the orchestrator model (`resolve_model(cfg.agent_model, cfg)`), heals the
saved history (`history.well_formed`), continues the step counter from
`store.max_step_idx(chat_id)`, then loops `agent.run_stream_events(...)`:

- Non-result events → `_on_event` translates `PartStartEvent`/`PartDeltaEvent` (token,
  thinking) and `FunctionToolCallEvent` (tool_call) into `StreamEvent`s; tools emit
  `action`/`observation` themselves.
- On `DeferredToolRequests` it collects approvals and **resumes** with `DeferredToolResults`.
- **Usage** accumulates `result.usage` (a *property* in pydantic-ai v2 — not a method) across
  segments; budgets enforced via `UsageLimits(tool_calls_limit=max_steps,
  total_tokens_limit=max_tokens)`.
- Terminal: emits a **`usage`** event (token/step/request counts) then `final`. On
  `CancelledError` it persists partial context (an `_Accum` of streamed text) + emits `usage`
  then `interrupted`; on `UsageLimitExceeded` it emits `usage` then a "Budget exceeded" error.

---

## `models_registry.py` — provider keys → Models

`build_model(spec, cfg)` / `resolve_model(spec, cfg)` / `build_vision_model(cfg)` take only a
`CoreConfig`. Keys come solely from `cfg.provider_keys`; if none is configured for the
provider, `build_model` **raises** (never falls back to an env key). `resolve_model` also
falls back to a provider you *do* have a key for, so a single-provider key works end-to-end
(orchestrator, sub-agents, and the `locate` vision model all map to it). No fast/smart/deep
tiers.

---

## The form-factors (thin shells over the core)

- **SDK** — `agenticbrowser/sdk.py`, `BrowserAgent`. Async context manager that mints
  session/chat ids, builds a `MemoryStore`/`SqliteStore` + `MemoryArtifacts` + a single-session
  `SessionRegistry` + a `Runner`. `run(goal) -> RunResult`, `stream(goal) -> AsyncIterator
  [StreamEvent]`, `approve=` callback (omit ⇒ auto-deny), `persist=`, `keys_from_env()`,
  `export_messages()`.
- **PydanticAI adapter** — `adapters/pydantic_ai.py`. `EphemeralBrowser(...).as_tool()` and
  `browse_task_tool(...)` expose a `browse_task(goal)` tool (one autonomous run behind one
  call; approvals handled inside via `approve=`).
- **MCP server** — `mcp/server.py` (FastMCP, `[mcp]` extra, `agenticbrowser-mcp` script).
  Tools `browse_task(goal, session_id="")`, `open_session`, `close_session`, `list_sessions`.
  Approval surfaces as MCP **elicitation**; config (incl. provider keys) via env; transports
  stdio / sse / streamable-http.
- **Self-host service** — `server/gateway.py` (extra `[server]`). See below.

---

## `server/gateway.py` — the self-host service

FastAPI app built from `cfg = settings().to_core_config()` in the lifespan, wiring a
`SessionRegistry`, `Recorder`, and `Runner` over the Postgres `Store`. All data routes require
auth (bearer header or `?token=` for WebSockets); users own their sessions/chats.

**REST (all under `/api`):**

- Auth: `POST /auth/register`, `POST /auth/login`, `GET /auth/me`, `POST /auth/logout`,
  `GET /auth/config`.
- Config: `GET /config` (browser provider, whether browserbase creds are required).
- Sessions: `POST /sessions`, `GET /sessions`, `GET /sessions/{id}`.
- Chats: `POST /chats`, `GET /chats`, `GET /chats/{id}`, `GET /chats/{id}/messages`,
  `GET /chats/{id}/steps`.
- Artifacts: `GET /artifacts/{chat_id}/{filename}` (ownership-checked).
- Health: `GET /health`.
- **Fire-and-forget runs (polling):** `POST /chats/{id}/runs` → `{run_id}`,
  `GET /runs/{id}` (status/output/usage/events), `POST /runs/{id}/approvals`,
  `POST /runs/{id}/stop`. For non-streaming consumers.

**WebSockets:**

- `WS /ws/chat/{chat_id}?token=…` — the streamed turn. Server → client: `StreamEvent`s
  (`ev.wire()`). Client → server: `{kind:"user_message", text}` (interrupts any in-flight
  turn), `{kind:"interrupt"}`, `{kind:"approval", decisions}`. A single sender task funnels
  all events through one queue so concurrent producers can't corrupt frames.
- `WS /ws/view/{session_id}?token=…` — live view + takeover (multi-tab). Server → client:
  `{type:"live_view", mode, url}`, `{type:"tabs", tabs, leases}`, and **binary** JPEG frames
  framed as `[version=1][width u16][height u16][tab_len u8][tab_id][jpeg]` (coalesced — only
  the newest frame ships). Client → server: `{kind:"watch", tab_id}`, `{kind:"tabs"}`,
  `{kind:"take_over"}`, `{kind:"mouse"|"key"|"scroll", tab_id, …}`, `{kind:"release", tab_id}`.

`SERVE_UI` (default true) mounts the bundled React build (`frontend/dist`) single-origin;
`SERVE_UI=false` runs it as a pure API.

`server/settings.py` (`Settings`, pydantic-settings) reads `.env` → `to_core_config()` (the
one producer of a `CoreConfig` for the gateway) and also `setdefault`s the provider keys into
`os.environ` so PydanticAI's env resolution works server-side (server-only; the core never
does this). Fields include `database_url, browser_provider, agent_model, worker_model,
headless, serve_ui, allow_registration, bootstrap_username/password, cors_origins,
artifacts_dir`, and the provider/Browserbase keys.

---

## How the pieces interact

**Streaming a turn.** The chat WS calls `runner.start_turn(session_id, chat_id, text, emit,
user_id)`. The runner ensures the session is live, resolves the model, and streams
`run_stream_events`; tools record steps and `emit` `action`/`observation`; the runner emits
`token`/`thinking`/`tool_call`, then a `usage` event, then `final`. The React reducer
(`frontend/src/chat/chatReducer.js`) commits the interleaved trail into the assistant message
on `final`/`interrupted`/`error` and embeds the turn's usage into that message; the sidebar
sums usage across messages for a session total.

**Approvals.** A destructive action raises `ApprovalRequired` → `DeferredToolRequests`. The
runner emits `approval_request` and waits; the answer (`submit_approval` / WS `approval` /
MCP elicitation / SDK `approve=`) resolves it and the turn **resumes** with
`DeferredToolResults`. Same seam across all form-factors.

**Steering / interrupts.** Sending a new message mid-run interrupts the in-flight turn,
which persists the partial `_Accum` text + an `interrupted` marker so the next turn builds on
it. The WS `interrupt` does the same without a new message.

**Swappable backend.** `cfg.browser_provider` flips local ↔ Browserbase; only `providers.py`
branches. Browserbase sessions can be reconnected by id across a restart.

**Persistence.** `storage_state` (auth cookies + localStorage), message history, the
Browserbase session id, and per-step screenshots/rows persist — so "log in once, run many
tasks" works and every run is replayable. In the SDK this is in-memory or a SQLite file; in
the server it's Postgres.

---

## Notes / sharp edges

- **Approval gate is a regex**, deliberately fail-safe but not a guarantee. It can miss
  unlabeled coordinate clicks and localized verbs; treat it as a safety rail. A stronger
  classifier (multilingual verbs, unlabeled-click fail-closed, URL scoping, spend ceiling)
  is planned future work.
- **`cost_usd` is `null`** until a per-model price table lands; `usage` carries token/step
  counts today.
- **Stateful server** — live sessions + per-tab leases live in memory; run a single replica
  or sticky-session by `session_id`. `storage_state`/steps persist to Postgres.
- **Sandboxing** — a headless embed runs a real browser visiting arbitrary pages next to your
  process; isolate untrusted targets (containerized Chromium / network namespace). The core
  uses only the keys you pass it, so a host env key stays out of the agent.
- **Playwright ↔ Chromium version skew** on the non-Docker path — pin Playwright and run the
  matching `playwright install`; the Docker image bakes the matched pair.
