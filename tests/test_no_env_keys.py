# SPDX-License-Identifier: Apache-2.0
"""The core must (1) import with no `.env`/DB and (2) use ONLY the keys it is given —
`CoreConfig.provider_keys` (the SDK's `keys=`, or the server's .env). There is no
ambient `os.environ` fallback and no per-session key, so a stray env key is never
silently used."""

from __future__ import annotations

import pytest

from agenticbrowser import CoreConfig
from agenticbrowser.models_registry import build_model


def test_provider_key_builds_a_model():
    cfg = CoreConfig(provider_keys={"anthropic": "sk-x"})
    model = build_model("anthropic:claude-sonnet-4-6", cfg)
    assert not isinstance(model, str)  # a concrete Model => a key was injected


def test_no_key_raises():
    # no configured key -> a clear error, NOT a silent env fallback.
    cfg = CoreConfig(provider_keys={})
    with pytest.raises(RuntimeError):
        build_model("anthropic:claude-sonnet-4-6", cfg)


def test_stray_env_key_is_not_used(monkeypatch):
    # even with ANTHROPIC_API_KEY set in the environment, the core never reads it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-STRAY-ENV-KEY")
    cfg = CoreConfig(provider_keys={})
    with pytest.raises(RuntimeError):
        build_model("anthropic:claude-sonnet-4-6", cfg)


def test_core_config_defaults_to_no_keys():
    # the embed/SDK default carries no keys until `keys=` is passed.
    cfg = CoreConfig()
    assert cfg.provider_keys == {}
