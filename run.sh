#!/usr/bin/env bash
#
# run.sh — bring up the whole Agentic Browser stack:
#   Postgres (docker compose) -> Python deps (uv) -> backend (uvicorn) -> frontend (Vite)
#
# Usage:
#   ./run.sh                 # start everything (installs deps as needed), Ctrl+C to stop
#   SKIP_INSTALL=1 ./run.sh  # skip dependency install/sync (faster restarts)
#   BACKEND_ONLY=1 ./run.sh  # backend + Postgres only (no Vite dev server)
#
set -euo pipefail
cd "$(dirname "$0")"

# ---- config (override via env) ---------------------------------------------
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
PG_PORT="${PG_PORT:-7935}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
BACKEND_ONLY="${BACKEND_ONLY:-0}"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- prerequisites ----------------------------------------------------------
command -v uv  >/dev/null 2>&1 || die "uv not found — install from https://docs.astral.sh/uv/"
if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
else die "docker compose not found"; fi

if [ "$BACKEND_ONLY" != "1" ]; then
  command -v npm >/dev/null 2>&1 || die "npm not found (needed for the frontend; use BACKEND_ONLY=1 to skip)"
fi

if [ ! -f .env ] || ! grep -q '^ANTHROPIC_API_KEY=..' .env 2>/dev/null; then
  [ -n "${ANTHROPIC_API_KEY:-}" ] || warn "ANTHROPIC_API_KEY not set in .env or environment — the agent will fail to call the model."
fi

# ---- 1. Postgres ------------------------------------------------------------
log "Starting Postgres (docker compose) on :$PG_PORT…"
$DC up -d

log "Waiting for Postgres to accept connections…"
for i in $(seq 1 60); do
  if $DC exec -T postgres pg_isready -U postgres -d postgres >/dev/null 2>&1; then
    echo "  Postgres ready."; break
  fi
  [ "$i" = 60 ] && die "Postgres did not become ready in time."
  sleep 1
done

# ---- 2. dependencies --------------------------------------------------------
if [ "$SKIP_INSTALL" != "1" ]; then
  log "Syncing Python deps (uv sync)…"
  uv sync

  if ! ls "$HOME"/.cache/ms-playwright/chromium-* >/dev/null 2>&1; then
    log "Installing Playwright Chromium (first run)…"
    uv run playwright install chromium
  fi

  if [ "$BACKEND_ONLY" != "1" ] && [ ! -d frontend/node_modules ]; then
    log "Installing frontend deps (npm install)…"
    (cd frontend && npm install)
  fi
else
  log "SKIP_INSTALL=1 — skipping dependency install/sync."
fi

# ---- 3. run -----------------------------------------------------------------
PIDS=()
_cleaned=0
cleanup() {
  [ "$_cleaned" = 1 ] && return; _cleaned=1
  log "Shutting down…"
  for pid in "${PIDS[@]:-}"; do kill -INT "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  echo "  Stopped. (Postgres container left running — '$DC down' to stop it.)"
}
trap cleanup INT TERM EXIT

log "Starting backend → http://$BACKEND_HOST:$BACKEND_PORT"
PYTHONPATH="$PWD" uv run uvicorn app.gateway:app \
  --host "$BACKEND_HOST" --port "$BACKEND_PORT" --reload &
PIDS+=($!)

if [ "$BACKEND_ONLY" != "1" ]; then
  log "Starting frontend → http://localhost:$FRONTEND_PORT"
  (cd frontend && npm run dev -- --port "$FRONTEND_PORT") &
  PIDS+=($!)
  printf '\n\033[1;32m==> Up!  UI: http://localhost:%s   API: http://%s:%s\033[0m\n' \
    "$FRONTEND_PORT" "$BACKEND_HOST" "$BACKEND_PORT"
else
  printf '\n\033[1;32m==> Up!  API: http://%s:%s\033[0m\n' "$BACKEND_HOST" "$BACKEND_PORT"
fi
echo "    Press Ctrl+C to stop."

# wait on the background jobs; if either exits, fall through to cleanup
wait
