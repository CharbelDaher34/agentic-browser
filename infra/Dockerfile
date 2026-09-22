# syntax=docker/dockerfile:1
# Agentic Browser — self-host image (Group 2). REST + WebSockets + (optional) UI.
#
# Build/run from the REPO ROOT (the build context), e.g. via the Makefile (`make up`):
#   docker build -t agenticbrowser -f infra/Dockerfile .
#   docker compose -f infra/docker-compose.yml up        # app + bundled Postgres
#
# Two stages: build the React UI, then run the FastAPI gateway on the Playwright
# base image (Chromium + system libs already present). Run from /app source so the
# bundled UI (/app/frontend/dist) is found (SERVE_UI=true).

# ---- 1) build the frontend ---------------------------------------------------
FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./frontend/
RUN cd frontend && (npm ci || npm install)
COPY frontend/ ./frontend/
RUN cd frontend && npm run build               # -> /ui/frontend/dist

# ---- 2) runtime --------------------------------------------------------------
# Playwright's Python image ships Chromium + all the apt libs (libnss3, fonts, …),
# but its system Python is whatever the Ubuntu base carries (3.12 on noble) — the
# project pins ==3.13.* (see uv.lock), so we let uv fetch its own 3.13 interpreter
# rather than installing into the system one.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PYTHONPATH=/app \
    BACKEND_PORT=8000 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN uv python install 3.13

# install deps via the [server]+[mcp] extras (build-system: hatchling), synced from
# uv.lock into /app/.venv. The project copy in site-packages is shadowed by the
# /app source on PYTHONPATH, so ROOT=/app and the bundled UI is found.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY agenticbrowser ./agenticbrowser
RUN uv sync --frozen --no-dev --extra server --extra mcp

# ensure the Chromium build matches the installed Playwright
RUN python -m playwright install chromium

COPY main.py ./
COPY --from=ui /ui/frontend/dist ./frontend/dist

EXPOSE 8000
# Chromium needs a real /dev/shm — compose sets shm_size; here we just run the app.
# --ws websockets-sansio: uvicorn's default ("auto") picks the deprecated
# websockets.legacy stack, whose keepalive-ping task drains the socket
# concurrently with our screencast sender. Under backpressure (big JPEG frames,
# slow client) that trips an AssertionError in its copied FlowControlMixin and
# silently kills keepalive for that connection. The sansio implementation has no
# such drain path.
CMD ["uvicorn", "agenticbrowser.server.gateway:app", "--host", "0.0.0.0", "--port", "8000", \
     "--ws", "websockets-sansio"]
