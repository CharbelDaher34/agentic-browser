"""Entrypoint shim for the FastAPI CLI (`fastapi deploy` / `fastapi run`).

The real ASGI app lives in `app/gateway.py`; the CLI auto-detects an `app`
object in a top-level module, so we just re-export it here.
"""

from app.gateway import app  # noqa: F401
