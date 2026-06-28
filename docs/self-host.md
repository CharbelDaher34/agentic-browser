# Agentic Browser — self-host (Docker)

Run the whole gateway (REST + WebSockets + optional bundled UI) yourself. Any
language calls it; the provider keys live in your `.env` and never leave your infra.

## Quick start

The Docker files live in `infra/` (build context is the repo root). A root
`Makefile` wraps the common operations — run `make help` for the full list.

```bash
make up        # build + start app on :8000 + bundled Postgres
# equivalently: docker compose -f infra/docker-compose.yml up --build
# open http://localhost:8000  (UI), or call the API directly
```

For local DB only (when running the backend from source via `make run` / `infra/run.sh`):

```bash
make db        # docker compose -f infra/docker-compose.yml up -d postgres
```

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | bundled pg | Postgres DSN (use a managed DB in prod) |
| `SERVE_UI` | `true` | serve the bundled React UI single-origin; `false` = pure API |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | — | provider keys (used for every session) |
| `BROWSER_PROVIDER` | `local` | `local` (Chromium in-image) or `browserbase` |
| `BROWSERBASE_API_KEY` / `BROWSERBASE_PROJECT_ID` | — | for the `browserbase` backend |
| `ALLOW_REGISTRATION` | `false` | public signups (leave off; use bootstrap) |
| `BOOTSTRAP_USERNAME` / `BOOTSTRAP_PASSWORD` | — | created on startup (idempotent) |

## API surface

- **Streaming (WebSocket):** `wss://host/ws/chat/{chat_id}?token=…` (tokens, steps,
  approval requests) and `wss://host/ws/view/{session_id}` (screencast + takeover).
- **Fire-and-forget (REST) — for non-streaming consumers (poll, no push):**
  - `POST /api/chats/{chat_id}/runs` `{text}` → `{run_id}` (runs in the background)
  - `GET  /api/runs/{run_id}` → `{status, output, usage, error, events}` (poll)
  - `POST /api/runs/{run_id}/approvals` `{decisions}` → resolve a paused destructive action
  - `POST /api/runs/{run_id}/stop` → cancel a run (unwinds cleanly, releases the lease)
  A run whose `GET` shows an `approval_request` is paused — answer via `/approvals`.
- Plus the existing `/api/auth`, `/api/sessions` (+ `/keys`, `/browserbase`),
  `/api/chats`, `/api/chats/{id}/messages|steps`, `/api/artifacts/...`.

## Image notes

- Built on `mcr.microsoft.com/playwright/python` (Chromium + system libs baked).
  `shm_size: 1gb` is set in compose (Chromium needs a real `/dev/shm`).
- `BROWSER_PROVIDER=browserbase` needs no in-image Chromium — a slimmer image is
  possible (drop the `playwright install` step).

## Production

- Point `DATABASE_URL` at a managed Postgres; drop the bundled `postgres` service.
- The container is **stateful** (live browser sessions + per-tab leases live in
  memory; storage_state + step trail persist to Postgres). Run a **single replica**
  or use **sticky sessions** per `session_id`.
- Set the provider key(s) matching `AGENT_MODEL` (and Browserbase creds if used);
  they live in the server's env and are used for every session.
