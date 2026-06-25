"""History processors — keep long conversations within the context window.

Registered on the agents via `capabilities=[ProcessHistory(...)]`. They run
BEFORE each model request and mutate only the per-request view of the history —
the persisted message blob (and thus the UI trail) keeps full fidelity.

Two passes, cheapest first:
  1. strip_old_screenshots — vision steps embed PNGs in tool returns; keep only
     the most recent few, replacing older images with a tiny text placeholder.
  2. compact_history — if still over a token budget, summarise older turns and
     keep the recent ones verbatim, cutting only at a real user-turn boundary so
     no tool-call/return pair is split.
"""

from __future__ import annotations

import dataclasses

from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

_KEEP_SCREENSHOTS = 3        # most-recent vision screenshots kept verbatim
_PLACEHOLDER = "[screenshot omitted to save context]"
_TOKEN_BUDGET = 120_000      # ~char/4 estimate above which we compact
_KEEP_TAIL = 8               # most-recent messages kept verbatim when compacting


def well_formed(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Longest prefix of `messages` in which every tool-call has a matching
    tool-return. Trims a trailing **dangling tool_use** (e.g. an approval-pending
    call left behind when a turn was interrupted) so the next model request isn't
    rejected with "tool_use without tool_result". Mid-list orphans (which only
    occur at a broken tail) are trimmed too."""
    open_ids: set[str] = set()
    safe = 0
    for i, msg in enumerate(messages):
        for p in getattr(msg, "parts", []):
            pk = getattr(p, "part_kind", "")
            if pk == "tool-call":
                open_ids.add(getattr(p, "tool_call_id", ""))
            elif pk == "tool-return":
                open_ids.discard(getattr(p, "tool_call_id", ""))
        if not open_ids:
            safe = i + 1
    return messages[:safe] if open_ids or safe != len(messages) else messages


def _is_image(item: object) -> bool:
    return isinstance(item, BinaryContent) and (item.media_type or "").startswith("image/")


def _part_has_image(part: object) -> bool:
    c = getattr(part, "content", None)
    if _is_image(c):
        return True
    return isinstance(c, (list, tuple)) and any(_is_image(i) for i in c)


def _strip_images(part: object) -> object:
    c = getattr(part, "content", None)
    if _is_image(c):
        return dataclasses.replace(part, content=_PLACEHOLDER)
    if isinstance(c, (list, tuple)):
        new = [(_PLACEHOLDER if _is_image(i) else i) for i in c]
        return dataclasses.replace(part, content=type(c)(new))
    return part


async def strip_old_screenshots(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Replace every embedded screenshot except the most recent few with a text
    placeholder. Keeps the part itself (and tool-call/return pairing) intact."""
    seen = 0
    out: list[ModelMessage] = []
    for msg in reversed(messages):
        parts = getattr(msg, "parts", None)
        if not parts:
            out.append(msg)
            continue
        new_parts = list(parts)
        changed = False
        for i, p in enumerate(parts):
            if _part_has_image(p):
                seen += 1
                if seen > _KEEP_SCREENSHOTS:
                    new_parts[i] = _strip_images(p)
                    changed = True
        out.append(dataclasses.replace(msg, parts=new_parts) if changed else msg)
    out.reverse()
    return out


def _estimate_tokens(messages: list[ModelMessage]) -> int:
    chars = 0
    for msg in messages:
        for p in getattr(msg, "parts", []):
            c = getattr(p, "content", "")
            if isinstance(c, str):
                chars += len(c)
            elif isinstance(c, (list, tuple)):
                chars += sum(len(i) for i in c if isinstance(i, str))
            chars += len(str(getattr(p, "args", "") or ""))
    return chars // 4


def _starts_user_turn(msg: ModelMessage) -> bool:
    """True if this message begins a fresh user turn (a request whose parts include
    a string user prompt) — a safe window boundary that never splits a tool pair."""
    if getattr(msg, "kind", "") != "request":
        return False
    for p in getattr(msg, "parts", []):
        if getattr(p, "part_kind", "") == "user-prompt" and isinstance(
            getattr(p, "content", None), str
        ):
            return True
    return False


def _summarize(messages: list[ModelMessage]) -> str:
    """Deterministic, cheap summary of the dropped span (no model call)."""
    users, assistants, tools = 0, 0, 0
    recent: list[str] = []
    for msg in messages:
        for p in getattr(msg, "parts", []):
            pk = getattr(p, "part_kind", "")
            if pk == "user-prompt" and isinstance(getattr(p, "content", None), str):
                users += 1
                recent.append("user: " + p.content[:160])
            elif pk == "text":
                assistants += 1
                recent.append("assistant: " + str(getattr(p, "content", ""))[:160])
            elif pk == "tool-call":
                tools += 1
    tail = "\n".join(recent[-6:])
    return (
        f"[Earlier conversation summary] {users} user messages, {assistants} "
        f"assistant replies, {tools} tool calls earlier in this chat. Most recent:\n{tail}"
    )


def _safe_cut(messages: list[ModelMessage], limit: int) -> int:
    """Largest index in (0, limit] where messages[:index] splits no tool-call/
    return pair. Prefers a user-turn boundary; otherwise falls back to the latest
    point where all tool calls are balanced, so a single huge tool-heavy turn
    (no intermediate user message) still compacts instead of being left oversized."""
    open_ids: set[str] = set()
    best_balanced = 0
    best_user = 0
    for i, msg in enumerate(messages):
        for p in getattr(msg, "parts", []):
            pk = getattr(p, "part_kind", "")
            if pk == "tool-call":
                open_ids.add(getattr(p, "tool_call_id", ""))
            elif pk == "tool-return":
                open_ids.discard(getattr(p, "tool_call_id", ""))
        idx = i + 1
        if idx > limit:
            break
        if not open_ids:
            best_balanced = idx
            if idx < len(messages) and _starts_user_turn(messages[idx]):
                best_user = idx
    return best_user or best_balanced


async def compact_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """When the history is large, replace older turns with one summary message and
    keep the most recent turns verbatim. Cuts only where no tool pair is split."""
    if len(messages) <= _KEEP_TAIL or _estimate_tokens(messages) < _TOKEN_BUDGET:
        return messages
    cut = _safe_cut(messages, len(messages) - _KEEP_TAIL)
    if cut <= 0:
        return messages
    summary = UserPromptPart(content=_summarize(messages[:cut]))
    head = messages[cut]
    if _starts_user_turn(head):
        # Fold the summary into the kept user turn so we don't emit two
        # consecutive user-role messages (which strict providers reject).
        return [dataclasses.replace(head, parts=[summary, *head.parts]), *messages[cut + 1:]]
    # head is an assistant/tool message — a standalone user summary alternates fine.
    return [ModelRequest(parts=[summary]), *messages[cut:]]
