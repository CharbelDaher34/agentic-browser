"""Recorder — turns each agent step into a durable, replayable record.

Screenshots go to an artifact store (local disk here; swap for S3/GCS in prod).
`put_png` returns a web path (`/api/artifacts/...`) that the gateway serves
through an ownership-checked route, so the frontend can render the replay trail.
The row in `steps` keeps the action, result, observation, and the artifact path;
together with the message history this is your full replay trail.
"""

from __future__ import annotations

import uuid

from .artifacts import ArtifactStore
from .models import Action, ActionResult, StepRecord
from .stores import Store


class Recorder:
    def __init__(self, store: Store, artifacts: ArtifactStore) -> None:
        self._store = store
        self._artifacts = artifacts

    async def record(
        self,
        chat_id: str,
        session_id: str,
        idx: int,
        action: Action,
        result: ActionResult,
        screenshot: bytes | None,
    ) -> None:
        uri = None
        if screenshot is not None:
            key = f"{chat_id}/{idx}-{uuid.uuid4().hex[:8]}"
            uri = await self._artifacts.put_png(key, screenshot)
        await self._store.insert_step(
            StepRecord(
                chat_id=chat_id,
                session_id=session_id,
                idx=idx,
                action=action,
                result=result,
                screenshot_uri=uri,
            )
        )
