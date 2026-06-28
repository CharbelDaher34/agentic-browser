# SPDX-License-Identifier: Apache-2.0
"""Agentic Browser — a hybrid DOM+vision browser-driving agent.

The package root exposes the dependency-light CORE surface so it imports with no
`.env`, no Postgres, and no FastAPI: configuration, the frozen event contract, the
Store/Artifact protocols and their in-memory/SQLite implementations, and the value
types. The agent/runner/registry (which pull in Playwright) and the higher-level
`BrowserAgent` SDK are imported from their submodules on demand.

The self-host gateway (multi-user, Postgres, REST/WS) lives under
`agenticbrowser.server` and is an optional extra (`pip install agenticbrowser[server]`).
"""

from .artifacts import ArtifactStore, LocalArtifacts, MemoryArtifacts, NullArtifacts
from .config import CoreConfig
from .events import EVENT_DATA_KEYS, EVENT_SCHEMA_VERSION, StreamEvent
from .models import Action, ActionKind, ActionResult, Risk, StepRecord
from .sdk import Approval, ApprovalRequest, BrowserAgent, RunResult
from .stores import MemoryStore, SqliteStore, Store

__all__ = [
    # SDK (M1)
    "BrowserAgent",
    "RunResult",
    "Approval",
    "ApprovalRequest",
    # config + contract
    "CoreConfig",
    "StreamEvent",
    "EVENT_SCHEMA_VERSION",
    "EVENT_DATA_KEYS",
    # stores + artifacts
    "Store",
    "MemoryStore",
    "SqliteStore",
    "ArtifactStore",
    "LocalArtifacts",
    "NullArtifacts",
    "MemoryArtifacts",
    # value types
    "Action",
    "ActionKind",
    "ActionResult",
    "Risk",
    "StepRecord",
]
