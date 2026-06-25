"""Value types. Same vocabulary as the design spec, plus the provider/live-view
modes and the StepRecord the recorder persists."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, Sequence


class Risk(str, Enum):
    SAFE = "safe"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    EXTRACT = "extract"


@dataclass(frozen=True, slots=True)
class Element:
    ref: str
    role: str
    name: str
    value: str | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PageObservation:
    url: str
    title: str
    elements: Sequence[Element]
    text_digest: str
    fingerprint: str

    def to_json(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "fingerprint": self.fingerprint,
            # Element is a slots dataclass (no __dict__), so build explicitly.
            "elements": [
                {
                    "ref": e.ref,
                    "role": e.role,
                    "name": e.name,
                    "value": e.value,
                    "enabled": e.enabled,
                }
                for e in self.elements
            ],
        }


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    risk: Risk = Risk.SAFE
    ref: str | None = None
    text: str | None = None
    url: str | None = None
    submit: bool = False

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "risk": self.risk.value,
            "ref": self.ref,
            "text": self.text,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    changed: bool
    observation: PageObservation
    error: str | None = None


# --- live control plane ------------------------------------------------------

ProviderName = Literal["local", "browserbase"]
DriverKind = Literal["agent", "human", "none"]
LiveViewMode = Literal["screencast", "iframe"]


@dataclass(frozen=True, slots=True)
class Lease:
    driver: DriverKind
    holder_id: str
    token: str


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One persisted step — observation + action + result + screenshot pointer."""

    chat_id: str
    session_id: str
    idx: int
    action: Action
    result: ActionResult
    screenshot_uri: str | None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    type: Literal[
        "token", "thinking", "tool_call", "tool_result", "action",
        "observation", "approval_request", "final", "error", "lease",
        "live_view",
    ]
    chat_id: str
    data: Mapping[str, Any] = field(default_factory=dict)
