"""Runtime configuration.

Loads `.env` from the project root *without* overriding real environment
variables, then exposes typed settings used across the app. Importing this
module also makes `ANTHROPIC_API_KEY` (and friends) visible to PydanticAI.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # don't clobber anything already exported in the real environment
        os.environ.setdefault(key, val)


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


class Settings:
    """Process-wide settings, read once from the environment."""

    def __init__(self) -> None:
        # asyncpg-compatible DSN; defaults to the docker-compose Postgres.
        self.database_url: str = os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:7935/postgres",
        )
        self.browser_provider: str = os.environ.get("BROWSER_PROVIDER", "local")
        self.agent_model: str = os.environ.get(
            "AGENT_MODEL", "anthropic:claude-sonnet-4-6"
        )
        self.headless: bool = _bool("HEADLESS", True)
        self.artifacts_dir: str = os.environ.get(
            "ARTIFACTS_DIR", str(ROOT / "artifacts")
        )
        # login-token lifetime (default 30 days)
        self.token_ttl_hours: int = int(os.environ.get("TOKEN_TTL_HOURS", "720"))
        self.cors_origins: list[str] = [
            o.strip()
            for o in os.environ.get(
                "CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if o.strip()
        ]
        # idle browser-session reaping
        self.idle_ttl_seconds: int = int(os.environ.get("IDLE_TTL_SECONDS", str(30 * 60)))


@lru_cache
def settings() -> Settings:
    return Settings()
