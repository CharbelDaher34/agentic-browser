"""Curated model registry + per-user (BYOK) model construction.

Models are chosen by a small **tier** menu (fast / smart / deep) rather than a
fixed provider, and resolved at runtime to whichever provider the user actually
has a key for. So a session with only an OpenAI key (or only Gemini, or only
Anthropic) works end to end — orchestrator, sub-agents, and the `locate` vision
model all map to that provider. `build_model()` turns a "provider:name" spec into
a concrete Model, injecting the right key (user key, else server key — unless
enforce_byok, which disables the server fallback).
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

# tier -> per-provider model. The orchestrator picks a tier; we resolve the
# provider from the available keys. Keep every provider able to serve every tier.
_TIERS: dict[str, dict[str, str]] = {
    "fast":  {"anthropic": "claude-sonnet-4-5-20250929", "openai": "gpt-5.4", "google": "gemini-3.5-flash"},
    "smart": {"anthropic": "claude-sonnet-4-6",          "openai": "gpt-5.4", "google": "gemini-3.5-flash"},
    "deep":  {"anthropic": "claude-opus-4-6",            "openai": "gpt-5.5", "google": "gemini-3.5-flash"},
}

# `locate` grounding model per provider. Google's robotics-ER is purpose-built for
# coordinates; OpenAI/Anthropic fall back to their flagship vision model.
_VISION = {
    "google": "gemini-robotics-er-1.6-preview",
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-4-6",
}

# Enum used as the `model_alias` tool-arg type so the schema constrains the LLM.
ModelAlias = Enum("ModelAlias", {t: t for t in _TIERS})


def alias_choices() -> list[str]:
    """The sub-agent model menu (drives the tool-arg enum + system prompt)."""
    return list(_TIERS)


def _server_key(keyname: str) -> str | None:
    attr = _SERVER_KEY_ATTR.get(keyname)
    return getattr(settings(), attr) if attr else None


def _pick_key(keyname: str, user_keys: dict | None) -> str | None:
    # user's own key wins; otherwise the server's key — UNLESS enforce_byok, in
    # which case there is no server fallback (only the user's own key is honored).
    user = (user_keys or {}).get(keyname)
    if user:
        return user
    if settings().enforce_byok:
        return None
    return _server_key(keyname)


def available_providers(user_keys: dict | None = None) -> list[str]:
    """Providers we have a usable key for, most-preferred first. Preference is the
    configured AGENT_MODEL's provider, then the rest — so a multi-key user keeps
    their expected default, while a single-key user just gets that provider."""
    prefer = settings().agent_model.split(":", 1)[0]
    order = [prefer] + [p for p in KEY_PROVIDERS if p != prefer]
    return [p for p in order if p in KEY_PROVIDERS and _pick_key(p, user_keys)]


def pick_model(tier: "str | ModelAlias", user_keys: dict | None = None) -> str:
    """Resolve a tier (fast/smart/deep) to a "provider:name" spec for the user's
    best available provider. Falls back to the configured AGENT_MODEL when no key
    is available (build_model then raises under enforce_byok, or resolves via env)."""
    t = tier.value if isinstance(tier, ModelAlias) else str(tier)
    if t not in _TIERS:
        t = "smart"
    provs = available_providers(user_keys)
    if not provs:
        return settings().agent_model
    p = provs[0]
    return f"{p}:{_TIERS[t][p]}"


def build_model(spec: str, user_keys: dict | None = None) -> Model | str:
    """Build a concrete Model from a "provider:name" spec, injecting the right
    API key (user key, else server key). Returns the spec string unchanged if no
    key is available (PydanticAI then resolves via env) — except under enforce_byok,
    where a missing user key raises so a server/env key can never be used."""
    provider, _, name = spec.partition(":")
    keyname = _KEYNAME.get(provider, provider)
    api_key = _pick_key(keyname, user_keys)
    if not api_key:
        if settings().enforce_byok:
            raise RuntimeError(
                f"No API key for '{provider}'. Add your {provider} key in this "
                f"session's settings — server keys are disabled (enforce_byok)."
            )
        return spec
    if provider == "anthropic":
        return AnthropicModel(name, provider=AnthropicProvider(api_key=api_key))
    if provider == "openai":
        return OpenAIChatModel(name, provider=OpenAIProvider(api_key=api_key))
    if provider in ("google", "google-gla"):
        return GoogleModel(name, provider=GoogleProvider(api_key=api_key))
    return spec


def build_vision_model(user_keys: dict | None = None) -> Model | str:
    """The `locate` grounding model, resolved to the user's available provider
    (Google's robotics-ER when present, else the provider's flagship vision model).
    Raises under enforce_byok when no key is available."""
    provs = available_providers(user_keys)
    p = provs[0] if provs else "google"
    return build_model(f"{p}:{_VISION[p]}", user_keys)
