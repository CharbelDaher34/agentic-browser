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

from ..config import CoreConfig

# repo root: this file is agenticbrowser/server/settings.py -> parents[2] = repo root
ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Process-wide settings, read once from env + .env."""

    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # reads the DATABASE_URL env var; a Supabase pooler DSN works too (Store.connect
    # normalizes the asyncpg driver + TLS).
    database_url: str = "postgresql://postgres:postgres@localhost:7935/postgres"
    browser_provider: str = "local"
    agent_model: str = "anthropic:claude-sonnet-4-6"
    worker_model: str | None = None  # sub-agents; None -> agent_model
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

    # THIS deployment's provider API keys — used for every browser session. Set at
    # least the one matching AGENT_MODEL's provider (and Browserbase if you use it).
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    browserbase_api_key: str | None = None
    browserbase_project_id: str | None = None

    # self-host service (Group 2 / Docker): serve the bundled React UI single-origin
    # (set false for a pure API deployment).
    serve_ui: bool = True

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

    def to_core_config(self) -> CoreConfig:
        """Project the server Settings onto the dependency-free CoreConfig the agent
        core consumes. The ONE place env-backed settings cross into the core."""
        return CoreConfig(
            browser_provider=self.browser_provider,
            headless=self.headless,
            browserbase_api_key=self.browserbase_api_key,
            browserbase_project_id=self.browserbase_project_id,
            agent_model=self.agent_model,
            worker_model=self.worker_model,
            # the server uses its own .env provider keys for every session
            provider_keys={
                "anthropic": self.anthropic_api_key,
                "openai": self.openai_api_key,
                "google": self.gemini_api_key,
            },
            max_subagent_depth=self.max_subagent_depth,
            max_concurrent_subagents=self.max_concurrent_subagents,
            max_tabs=self.max_tabs,
            idle_ttl_seconds=self.idle_ttl_seconds,
            screencast_quality=self.screencast_quality,
            screencast_every_nth_frame=self.screencast_every_nth_frame,
            screencast_max_width=self.screencast_max_width,
            screencast_max_height=self.screencast_max_height,
        )


@lru_cache
def settings() -> Settings:
    s = Settings()
    # Make the server keys visible to PydanticAI's env-based resolution so models
    # constructed from a plain "provider:name" string still find a key.
    for var, val in (
        ("ANTHROPIC_API_KEY", s.anthropic_api_key),
        ("OPENAI_API_KEY", s.openai_api_key),
        ("GEMINI_API_KEY", s.gemini_api_key),
    ):
        if val:
            os.environ.setdefault(var, val)
    return s
