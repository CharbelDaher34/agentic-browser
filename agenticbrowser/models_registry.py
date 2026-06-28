"""Model construction from a provider key.

A model is a "provider:name" spec. `build_model()` turns it into a concrete Model,
injecting the key from `CoreConfig.provider_keys`. `resolve_model()` additionally
falls back to a provider we DO have a key for, so a process with only one provider's
key (OpenAI, Gemini, or Anthropic) works end to end — orchestrator (`agent_model`),
sub-agents (`worker_model`), and the `locate` vision model all map to that provider.
No fast/smart/deep tiers: the orchestrator and sub-agent models are set explicitly.

`provider_keys` is the ONLY source of keys — filled by the SDK from its `keys=`
constructor arg, or by the self-host server from its .env. Nothing here reads
`os.environ` directly, so a stray ambient key is never silently used.
"""

from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from .config import CoreConfig

# The providers a key can be supplied for. CoreConfig.provider_keys uses these
# same names (the gateway derives its validation set from KEY_PROVIDERS).
KEY_PROVIDERS = ("anthropic", "openai", "google")

# provider -> the conventional env var holding its API key (one source of truth;
# the SDK's keys_from_env() reads this).
KEY_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
}

# spec provider prefix -> the key name used in a user's keys dict / server settings
_KEYNAME = {"anthropic": "anthropic", "openai": "openai", "google": "google", "google-gla": "google"}

# One default model per provider — used ONLY to resolve a usable model when the
# caller's configured model is for a provider they hold no key for (so a single
# provider key keeps working). There are NO fast/smart/deep tiers: the orchestrator
# runs on `agent_model` and sub-agents on `worker_model` (or agent_model); callers set these.
_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.4",
    "google": "gemini-3.5-flash",
}

# `locate` grounding model per provider. Google's robotics-ER is purpose-built for
# coordinates; OpenAI/Anthropic fall back to their flagship vision model.
_VISION = {
    "google": "gemini-robotics-er-1.6-preview",
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-4-6",
}


def _pick_key(keyname: str, cfg: CoreConfig) -> str | None:
    # the ONLY source of keys: this process's configured provider_keys. The SDK
    # fills it from its `keys=` arg; the self-host server from its .env. The core
    # never reads os.environ, so a stray ambient key is never used.
    return (cfg.provider_keys or {}).get(keyname)


def available_providers(cfg: CoreConfig) -> list[str]:
    """Providers we have a usable key for, most-preferred first. Preference is the
    configured agent_model's provider, then the rest — so a multi-key process keeps
    its expected default, while a single-key one just gets that provider."""
    prefer = cfg.agent_model.split(":", 1)[0]
    order = [prefer] + [p for p in KEY_PROVIDERS if p != prefer]
    return [p for p in order if p in KEY_PROVIDERS and _pick_key(p, cfg)]


def resolve_model(spec: str, cfg: CoreConfig) -> Model | str:
    """Build the model for `spec` ("provider:name"). If there is no usable key for
    that provider, fall back to the default model of a provider we DO have a key for
    — so a process with one provider key keeps working without any tier menu."""
    provider = spec.split(":", 1)[0]
    keyname = _KEYNAME.get(provider, provider)
    if _pick_key(keyname, cfg):
        return build_model(spec, cfg)
    provs = available_providers(cfg)
    if provs:
        p = provs[0]
        return build_model(f"{p}:{_DEFAULT_MODEL[p]}", cfg)
    return build_model(spec, cfg)  # no key anywhere -> build_model raises


def build_model(spec: str, cfg: CoreConfig) -> Model | str:
    """Build a concrete Model from a "provider:name" spec, injecting the configured
    `provider_keys` key. Raises if no key is available for the provider — an ambient
    env key is never silently used."""
    provider, _, name = spec.partition(":")
    keyname = _KEYNAME.get(provider, provider)
    api_key = _pick_key(keyname, cfg)
    if not api_key:
        raise RuntimeError(
            f"No API key for '{provider}'. Provide a {provider} key "
            f"(SDK: keys={{'{keyname}': ...}}; server: set the matching env var)."
        )
    if provider == "anthropic":
        return AnthropicModel(name, provider=AnthropicProvider(api_key=api_key))
    if provider == "openai":
        return OpenAIChatModel(name, provider=OpenAIProvider(api_key=api_key))
    if provider in ("google", "google-gla"):
        return GoogleModel(name, provider=GoogleProvider(api_key=api_key))
    return spec


def build_vision_model(cfg: CoreConfig) -> Model | str:
    """The `locate` grounding model, resolved to the available provider (Google's
    robotics-ER when present, else the provider's flagship vision model). Raises
    when no key is available."""
    provs = available_providers(cfg)
    p = provs[0] if provs else "google"
    return build_model(f"{p}:{_VISION[p]}", cfg)
