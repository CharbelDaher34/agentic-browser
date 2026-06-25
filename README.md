# Agentic Browser Chatbot

An AI agent that **drives a real web browser** to accomplish your goals, with a
live chat UI, a watch‑along browser view, and human‑in‑the‑loop approval for
risky actions.

- **Backend** — FastAPI + **PydanticAI v2** + Playwright
- **Persistence** — Postgres (users, login sessions, browser sessions, chats,
  message history, recorded steps + screenshots)
- **Multi‑user** — accounts with login tokens; every browser session and chat is
  scoped to its owner
- **Streaming** — model tokens, thinking, tool calls, and browser steps stream
  live over WebSockets
- **Live view + takeover** — watch the agent's browser (screencast) and grab
  control at any time; a single‑driver *lease* pauses the agent while you drive
- **Approval gate** — destructive actions (pay/delete/send/…) pause and wait for
  your approval before executing
- **Swappable backend** — `local` Playwright (own CDP screencast) or
  **Browserbase** (managed iframe live view), flipped by one env var
- **Frontend** — Vite + React

## Architecture

```
app/
  config.py     env/.env settings (DB url, provider, model, …)
  models.py     value types + StepRecord + StreamEvent
  providers.py  local Playwright vs Browserbase, behind one interface
  session.py    PlaywrightSession: perceive, act/verify, screencast, input inject
  store.py      Postgres: users, auth tokens, sessions, chats, messages, steps
  auth.py       pbkdf2 password hashing + bearer login tokens + FastAPI deps
  recorder.py   per-step screenshot artifact + DB row (replay trail)
  registry.py   long-lived browser sessions + single-driver control lease
  agent.py      PydanticAI v2 agent + tools (record + emit + approval gate)
  runner.py     run_stream_events loop: tokens + steps + approvals + persist
  gateway.py    FastAPI: REST + chat WS + view/takeover WS + static serving
  evals.py      pydantic_evals dataset + success evaluators
frontend/
  src/App.jsx   auth + workspace (sidebar of sessions/chats)
  src/Chat.jsx  streaming transcript, activity timeline, approvals, step trail
  src/LiveView.jsx  screencast canvas / iframe + takeover + input forwarding
  src/api.js    REST client + WS url helper (token-aware)
```

### Data model (users **and** sessions)

| table           | purpose                                                        |
|-----------------|----------------------------------------------------------------|
| `users`         | accounts (username + pbkdf2 password hash)                     |
| `auth_sessions` | login bearer tokens → user, with expiry                       |
| `sessions`      | **browser** sessions: provider + latest `storage_state`, owner |
| `chats`         | chat → (browser session, user) binding + title                |
| `messages`      | PydanticAI message history per chat (resumes full context)     |
| `steps`         | every agent step (action + result + screenshot pointer)        |

"Sessions" exist in two senses, both supported: user **login sessions** and the
agent's long‑lived **browser sessions** (one browser can back many chats).

## Prerequisites

- Python 3.13 + [`uv`](https://docs.astral.sh/uv/)
- Node 18+ (uses Vite 5)
- Docker (for Postgres) — or your own Postgres
- An `ANTHROPIC_API_KEY` (the agent uses `anthropic:claude-sonnet-4-6`)

## Setup

```bash
# 1) Postgres (docker-compose maps it to localhost:7935)
docker compose up -d

# 2) Python deps (uv-managed venv from pyproject.toml / uv.lock)
uv sync
uv run playwright install chromium      # local provider needs Chromium

# 3) Frontend deps
cd frontend && npm install && cd ..
```

Configuration is read from the environment and `.env` (see `app/config.py`).
Defaults already match the docker‑compose Postgres:

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:7935/postgres
BROWSER_PROVIDER=local                 # or: browserbase
AGENT_MODEL=anthropic:claude-sonnet-4-6
HEADLESS=true
ANTHROPIC_API_KEY=sk-ant-...           # required
# browserbase only:
# BROWSERBASE_API_KEY=...  BROWSERBASE_PROJECT_ID=...
```

## Run

### One command (recommended)

```bash
./run.sh
```

`run.sh` starts Postgres (docker compose), syncs Python deps, installs Chromium
and the frontend deps on first run, then launches the backend and the Vite dev
server, and tears them down cleanly on Ctrl+C. Options:

```bash
SKIP_INSTALL=1 ./run.sh   # faster restarts (skip dep sync/install)
BACKEND_ONLY=1 ./run.sh   # backend + Postgres only (no Vite dev server)
```

### Or run the pieces yourself

```bash
# backend (serves the API + WebSockets on :8000; also serves frontend/dist if built)
uv run uvicorn app.gateway:app --host 0.0.0.0 --port 8000 --reload

# frontend dev server on :5173 (proxies /api, /artifacts, /ws -> :8000)
cd frontend && npm run dev
```

Open **http://localhost:5173**, register an account, click **+ New browser
session**, then **+ chat**, and tell the agent what to do
(e.g. *"go to Hacker News and tell me the top story"*).

For a single‑origin production build: `cd frontend && npm run build` — the
gateway then serves the built app at `/` on :8000.

## API (REST, all under `/api`, bearer auth except register/login)

| method | path | purpose |
|---|---|---|
| POST | `/api/auth/register` · `/login` | → `{token, user}` |
| GET  | `/api/auth/me` · POST `/logout` | current user · invalidate token |
| POST | `/api/sessions` | create a browser session (opens the browser) |
| GET  | `/api/sessions` · `/api/sessions/{id}` | list · detail + live state |
| POST | `/api/chats` | create a chat bound to a session |
| GET  | `/api/chats[?session_id=]` | list my chats |
| GET  | `/api/chats/{id}/messages` · `/steps` | transcript · replay trail |

### WebSockets (token via `?token=`)

`/ws/chat/{chat_id}` — client sends:
```json
{"kind": "user_message", "text": "book the cheapest flight to NYC"}
{"kind": "approval", "decisions": {"call_abc": true, "call_xyz": false}}
```
server streams: `token`, `thinking`, `tool_call`, `tool_result`, `action`,
`observation`, `approval_request`, `final`, `error`.

`/ws/view/{session_id}` — server first sends
`{"type":"live_view","mode":"screencast"|"iframe","url":...}`, then (screencast)
`{"type":"frame","data":"<base64 jpeg>"}`. Client sends `take_over` / `release`
and, in screencast mode, `mouse` / `key` events.

## Evals

`app/evals.py` is a `pydantic_evals` suite (goal + checkable success criterion)
that runs the agent end‑to‑end. Wire a `Store`/`Recorder`/`SessionRegistry` and
call `run_evals(...)`; run it in CI on every prompt/model/provider change.

## Notes / sharp edges

- `_classify` (destructive‑action detection) is keyword‑based — a placeholder.
  Real detection should inspect the resolved element/page, not substrings.
- The `local` provider runs headless Chromium and streams CDP screencast frames;
  human takeover injects CDP input. Browserbase exposes an embeddable iframe with
  built‑in takeover instead; the lease still pauses the agent in both modes.
- `storage_state` (cookies) is saved per session at turn end / idle‑reap and
  restored on (re)open, so logins survive restarts.
