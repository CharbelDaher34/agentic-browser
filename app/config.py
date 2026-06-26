"""Runtime configuration (typed, via pydantic-settings).

Reads from the environment and the project `.env`. Real env vars take precedence
over `.env`. After building, the provider API keys are also pushed into
`os.environ` (setdefault) so PydanticAI's env-based model resolution and the
server-key fallback keep working.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Process-wide settings, read once from env + .env."""

    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # core
    database_url: str = "postgresql://postgres:postgres@localhost:7935/postgres"
    browser_provider: str = "local"
    agent_model: str = "anthropic:claude-sonnet-4-6"
    headless: bool = True
    artifacts_dir: str = str(ROOT / "artifacts")
    token_ttl_hours: int = 1  # 30 days
    # access control: lock a public deploy to a single account. Registration is
    # OFF by default; the bootstrap user (if set) is created on startup.
    allow_registration: bool = False
    bootstrap_username: str | None = None
    bootstrap_password: str | None = None
    # NoDecode: keep pydantic-settings from JSON-parsing the env value so the
    # `_split_csv` validator below can accept a plain comma-separated string.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173", "http://127.0.0.1:5173",
    ]
    idle_ttl_seconds: int = 30 * 60

    # sub-agent / multi-tab guards (prevent fork-bombs + runaway tabs)
    max_subagent_depth: int = 1
    max_concurrent_subagents: int = 1
    max_tabs: int = 6

    # server-side provider API keys (BYOK falls back to these). On a public deploy
    # these are left unset so every user must bring their own (Browserbase too).
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    browserbase_api_key: str | None = None
    browserbase_project_id: str | None = None
    # Enforce BYOK: when true, the server keys above are NEVER used as a fallback —
    # every model call AND Browserbase session must use the user's own per-session
    # keys. Set this on a public deploy so a forgotten server key is never served.
    enforce_byok: bool = False
    # symmetric key for encrypting per-user API keys at rest (see app/crypto.py)
    app_secret: str | None = None

    # screencast / live-view tuning
    screencast_quality: int = 60
    screencast_every_nth_frame: int = 1
    screencast_max_width: int | None = None
    screencast_max_height: int | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("headless", mode="before")
    @classmethod
    def _blank_is_default(cls, v):
        # a blank env value (e.g. `HEADLESS=` left in a template) means "unset" —
        # fall back to the default rather than failing strict bool parsing.
        if isinstance(v, str) and not v.strip():
            return True
        return v


@lru_cache
def settings() -> Settings:
    s = Settings()
    # Make the server keys visible to PydanticAI's env-based resolution + the
    # GEMINI/GOOGLE lookups, so models constructed from plain "provider:name"
    # strings (and the server-fallback path) still find a key. Skipped entirely
    # under enforce_byok so PydanticAI can't silently resolve a server key.
    if not s.enforce_byok:
        for var, val in (
            ("ANTHROPIC_API_KEY", s.anthropic_api_key),
            ("OPENAI_API_KEY", s.openai_api_key),
            ("GEMINI_API_KEY", s.gemini_api_key),
        ):
            if val:
                os.environ.setdefault(var, val)
    return s
