#!/usr/bin/env bash
#
# run.sh — bring up the whole Agentic Browser stack:
#   Postgres (docker compose) -> Python deps (uv) -> build frontend -> backend
#   (uvicorn serves the UI + API on ONE port).
#
# Usage:
#   ./run.sh                 # build UI + serve everything on :BACKEND_PORT (one origin)
#   DEV=1 ./run.sh           # hot-reload dev: Vite UI on :FRONTEND_PORT, proxy to backend
#   SKIP_INSTALL=1 ./run.sh  # skip dependency install/sync (faster restarts)
#   BACKEND_ONLY=1 ./run.sh  # backend + Postgres only (no frontend build/serve)
#
set -euo pipefail
cd "$(dirname "$0")"

# ---- config (override via env) ---------------------------------------------
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
PG_PORT="${PG_PORT:-7935}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
BACKEND_ONLY="${BACKEND_ONLY:-0}"
# Default: FastAPI serves the built frontend on the backend port (one origin, no
# Vite/proxy). DEV=1 instead runs the Vite dev server (hot reload) on its own
# port, proxying /api and /ws to the backend.
DEV="${DEV:-0}"

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# kill whatever is holding a TCP port (tolerant of nothing being there)
free_port() {
  local port="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  elif command -v lsof >/dev/null 2>&1; then
    local pids; pids=$(lsof -ti "tcp:${port}" 2>/dev/null || true)
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
  fi
}

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
# free the app ports first so a stale backend/frontend doesn't block startup
# (Postgres on :$PG_PORT is intentionally left alone).
log "Freeing app ports…"
free_port "$BACKEND_PORT"
if [ "$BACKEND_ONLY" != "1" ]; then
  free_port "$FRONTEND_PORT"
fi
sleep 1   # let the OS release the sockets

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

# Default (single origin): build the frontend now so FastAPI's app.frontend()
# picks up dist/ at import and serves the UI + API on one port. DEV uses Vite.
if [ "$DEV" != "1" ] && [ "$BACKEND_ONLY" != "1" ]; then
  log "Building frontend → served by FastAPI on :$BACKEND_PORT"
  (cd frontend && npm run build)
fi

RELOAD=""; [ "$DEV" = "1" ] && RELOAD="--reload"
log "Starting backend → http://$BACKEND_HOST:$BACKEND_PORT"
PYTHONPATH="$PWD" uv run uvicorn app.gateway:app \
  --host "$BACKEND_HOST" --port "$BACKEND_PORT" $RELOAD &
PIDS+=($!)

if [ "$DEV" = "1" ] && [ "$BACKEND_ONLY" != "1" ]; then
  # Hot-reload mode: Vite serves the UI on its own port and proxies /api + /ws to
  # the backend. Wait for the backend first so the UI's first /api calls don't hit
  # a not-yet-listening port (the ECONNREFUSED proxy noise). Best-effort, ~30s.
  log "Waiting for backend to be ready…"
  for _ in $(seq 1 60); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$BACKEND_PORT") 2>/dev/null; then
      exec 3>&- 3<&- 2>/dev/null || true
      break
    fi
    sleep 0.5
  done
  log "Starting frontend (Vite, hot reload) → http://localhost:$FRONTEND_PORT"
  (cd frontend && npm run dev -- --port "$FRONTEND_PORT") &
  PIDS+=($!)
  printf '\n\033[1;32m==> Up!  UI: http://localhost:%s   API: http://%s:%s\033[0m\n' \
    "$FRONTEND_PORT" "$BACKEND_HOST" "$BACKEND_PORT"
elif [ "$BACKEND_ONLY" != "1" ]; then
  # single origin: FastAPI serves the built UI AND the API on one port.
  printf '\n\033[1;32m==> Up!  App + API: http://%s:%s  (one port, served by FastAPI)\033[0m\n' \
    "$BACKEND_HOST" "$BACKEND_PORT"
else
  printf '\n\033[1;32m==> Up!  API: http://%s:%s\033[0m\n' "$BACKEND_HOST" "$BACKEND_PORT"
fi
echo "    Press Ctrl+C to stop."

# wait on the background jobs; if either exits, fall through to cleanup
wait
