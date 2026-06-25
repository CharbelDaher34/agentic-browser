"""Curated model registry + per-user (BYOK) model construction.

The orchestrator picks a sub-agent's model from a small, safe menu (aliases) so
it can never hallucinate a provider/model id. `build_model()` turns a
"provider:name" spec into a concrete Model, injecting the per-user API key for
that provider when present and otherwise falling back to the server key (or
PydanticAI's env-based resolution). `locate()` uses the dedicated vision model
(gemini-robotics-er), kept out of the sub-agent menu.
"""

from __future__ import annotations

import os
from enum import Enum

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from .config import settings

# alias -> pydantic-ai model spec (provider:name). "smart" matches the orchestrator default.
_ALIASES: dict[str, str] = {
    "fast": "google:gemini-2.5-flash",       # cheap/quick lookups & simple side-tasks
    "smart": "anthropic:claude-sonnet-4-6",  # balanced default
    "deep": "anthropic:claude-opus-4-8",     # hard, multi-step reasoning side-quests
    "gpt": "openai:gpt-4.1",                  # OpenAI flagship
    "gpt-mini": "openai:gpt-4.1-mini",        # OpenAI cheap/fast
}

# Enum used as the `model_alias` tool-arg type so the schema constrains the LLM.
ModelAlias = Enum("ModelAlias", {k: k for k in _ALIASES})

_VISION_MODEL_NAME = "gemini-robotics-er-1.6-preview"

# Canonical BYOK provider table: key name -> the `Settings` attr holding the
# server-side fallback key. The single source of truth for "which providers a
# user can supply a key for" — the gateway derives its validation set from this.
_SERVER_KEY_ATTR = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "google": "gemini_api_key",
}
KEY_PROVIDERS = tuple(_SERVER_KEY_ATTR)

# spec provider prefix -> the key name used in a user's keys dict / server settings
_KEYNAME = {"anthropic": "anthropic", "openai": "openai", "google": "google", "google-gla": "google"}


def alias_choices() -> list[str]:
    """The sub-agent model menu (drives the tool-arg enum + system prompt)."""
    return list(_ALIASES)


def resolve(alias: "str | ModelAlias") -> str:
    """alias -> a "provider:name" spec. Unknown -> safe 'smart'."""
    key = alias.value if isinstance(alias, ModelAlias) else str(alias)
    return _ALIASES.get(key, _ALIASES["smart"])


def _server_key(keyname: str) -> str | None:
    attr = _SERVER_KEY_ATTR.get(keyname)
    return getattr(settings(), attr) if attr else None


def _pick_key(keyname: str, user_keys: dict | None) -> str | None:
    # user's own key wins; otherwise the server's key (BYOK = optional fallback)
    return (user_keys or {}).get(keyname) or _server_key(keyname)


def build_model(spec: str, user_keys: dict | None = None) -> Model | str:
    """Build a concrete Model from a "provider:name" spec, injecting the right
    API key (user key, else server key). Returns the spec string unchanged if the
    provider is unknown or no key is available (PydanticAI then resolves via env)."""
    provider, _, name = spec.partition(":")
    keyname = _KEYNAME.get(provider, provider)
    api_key = _pick_key(keyname, user_keys)
    if not api_key:
        return spec
    if provider == "anthropic":
        return AnthropicModel(name, provider=AnthropicProvider(api_key=api_key))
    if provider == "openai":
        return OpenAIChatModel(name, provider=OpenAIProvider(api_key=api_key))
    if provider in ("google", "google-gla"):
        return GoogleModel(name, provider=GoogleProvider(api_key=api_key))
    return spec


def build_vision_model(user_keys: dict | None = None) -> GoogleModel:
    """Gemini Robotics-ER (the `locate` grounding model) with the user's Google
    key when set, else the server key / env."""
    api_key = (
        _pick_key("google", user_keys)
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    return GoogleModel(_VISION_MODEL_NAME, provider=GoogleProvider(api_key=api_key))
