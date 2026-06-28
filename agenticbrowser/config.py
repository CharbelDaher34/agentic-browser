# SPDX-License-Identifier: Apache-2.0
"""Core runtime configuration — a plain, dependency-free dataclass.

`CoreConfig` is what the agent core (agent / runner / registry / session /
providers / models_registry) actually reads. It replaces the old
`from .config import settings` process-global so the core runs headless, in any
process, with NO `.env` and NO pydantic-settings dependency.

Two producers build one:
  • the embeddable SDK builds a `CoreConfig` directly from constructor args;
  • the server's `Settings` (pydantic-settings) builds one via
    `Settings.to_core_config()` (see `agenticbrowser/server/settings.py`).

Server-only concerns (database URL, CORS, auth token TTL, artifacts dir,
registration/bootstrap) deliberately live in `Settings`, NOT here — the core
never needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CoreConfig:
    """Everything the agent core needs to run, injected (never read from env)."""

    # --- browser backend ---
    browser_provider: str = "local"                 # "local" | "browserbase"
    headless: bool = True
    browserbase_api_key: str | None = None          # the Browserbase creds this process uses
    browserbase_project_id: str | None = None

    # --- models / provider keys ---
    agent_model: str = "anthropic:claude-sonnet-4-6"   # the orchestrator
    worker_model: str | None = None                    # sub-agents; None -> agent_model
    # provider -> the API key this process uses, e.g.
    # {"anthropic": "...", "openai": ..., "google": ...}. The SDK fills this from its
    # `keys=` constructor arg; the self-host server fills it from its own .env. These
    # are the ONLY keys the core uses — it never reads os.environ, so a stray ambient
    # key is never silently used.
    provider_keys: dict[str, str | None] = field(default_factory=dict)

    # --- agent limits (fork-bomb / runaway-tab guards) ---
    max_subagent_depth: int = 1
    max_concurrent_subagents: int = 1
    max_tabs: int = 6

    # --- session lifecycle / live-view tuning ---
    idle_ttl_seconds: int = 30 * 60
    screencast_quality: int = 60
    screencast_every_nth_frame: int = 1
    screencast_max_width: int | None = None
    screencast_max_height: int | None = None

    # --- budgets (cost observability, W-C) ---
    max_steps: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
