"""Value types. Same vocabulary as the design spec, plus the provider/live-view
modes and the StepRecord the recorder persists."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Sequence

# StreamEvent now lives in events.py (the frozen, versioned wire contract).
# Re-exported here for backwards-compatible `from .models import StreamEvent`.
from .events import StreamEvent  # noqa: F401


class Risk(str, Enum):
    SAFE = "safe"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


class ActionKind(str, Enum):
    # DOM-ref based (act on elements by their ref id)
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    EXTRACT = "extract"
    # vision / coordinate based (act on pixels of the screenshot) + navigation,
    # ported from the Computer-Use interface in computers/
    CLICK_AT = "click_at"
    TYPE_AT = "type_at"
    SCROLL_AT = "scroll_at"
    DRAG = "drag"
    KEY = "key"
    BACK = "back"
    FORWARD = "forward"
    WAIT = "wait"


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
    # coordinate / vision params (pixel space of the screenshot)
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    keys: str | None = None          # e.g. "Enter" or "Control+A"
    direction: str | None = None     # up|down|left|right for scrolls
    magnitude: int | None = None
    seconds: float | None = None
    clear: bool = True               # type_at: clear the field before typing

    def to_json(self) -> dict:
        out: dict = {"kind": self.kind.value, "risk": self.risk.value}
        for k in ("ref", "text", "url", "x", "y", "x2", "y2", "keys", "direction",
                  "submit", "magnitude", "seconds", "clear"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        return out


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


# (StreamEvent is defined in events.py and re-exported at the top of this module.)
