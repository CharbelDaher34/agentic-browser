# SPDX-License-Identifier: Apache-2.0
"""Artifact stores — where per-step screenshots go.

`ArtifactStore.put_png(key, data) -> uri` is the only seam the `Recorder` needs.
Implementations:

  • LocalArtifacts  — write PNGs to disk; returns the gateway's `/api/artifacts/...`
                      web path (used by the server form-factor).
  • NullArtifacts   — drop the bytes; returns a synthetic uri (headless, no disk).
  • MemoryArtifacts — keep the bytes in-process so an embed can surface them on
                      RunResult.steps[i].screenshot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ArtifactStore(Protocol):
    async def put_png(self, key: str, data: bytes) -> str: ...


class LocalArtifacts:
    def __init__(self, root: str = "artifacts") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    async def put_png(self, key: str, data: bytes) -> str:
        path = self._root / f"{key}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        # web path served by the gateway's authenticated /api/artifacts route
        # (key is "<chat_id>/<idx>-<hash>"); ownership is checked on fetch.
        return f"/api/artifacts/{key}.png"


class NullArtifacts:
    """Drop screenshots entirely — for headless runs that don't need a replay trail."""

    async def put_png(self, key: str, data: bytes) -> str:
        return f"mem://{key}.png"


class MemoryArtifacts:
    """Keep screenshot bytes in-process, addressable by uri (embed/eval use)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put_png(self, key: str, data: bytes) -> str:
        uri = f"mem://{key}.png"
        self.blobs[uri] = data
        return uri
