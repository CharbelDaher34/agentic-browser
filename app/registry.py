"""SessionRegistry — named, long-lived browser sessions + the single-driver lease.

Sessions outlive chats. On create we restore persisted storage_state so auth
survives restarts. `ensure()` lazily re-opens a session that exists in the DB
but isn't live in memory (e.g. after a server restart). The lease guarantees one
driver; takeover clears the agent_may_drive event so in-flight agent tools block
instead of racing a human.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from .config import settings
from .models import DriverKind, Lease, ProviderName
from .providers import make_provider
from .session import PlaywrightSession
from .store import Store


@dataclass
class _Entry:
    session: PlaywrightSession
    provider: ProviderName
    lease: Lease
    chats: set[str] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)
    agent_may_drive: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionRegistry:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._sessions: dict[str, _Entry] = {}
        self._create_lock = asyncio.Lock()

    async def create(
        self, session_id: str, provider_name: ProviderName | None = None
    ) -> str:
        async with self._create_lock:
            if session_id in self._sessions:
                return session_id
            provider = make_provider(provider_name)
            # restore the full storage_state (cookies + localStorage) from a
            # previous run, applied at context creation so auth fully survives.
            prior = await self._store.load_storage_state(session_id)
            session = await PlaywrightSession.open(provider, storage_state=prior)
            await self._store.upsert_session(session_id, provider.name)
            self._sessions[session_id] = _Entry(
                session=session, provider=provider.name, lease=Lease("none", "", "")
            )
            return session_id

    async def ensure(self, session_id: str) -> bool:
        """Make sure the session is live in memory; rehydrate from DB if needed.

        Returns False if the session_id is unknown to the DB."""
        if session_id in self._sessions:
            return True
        row = await self._store.get_session(session_id)
        if not row:
            return False
        await self.create(session_id, row["provider"])
        return True

    def is_live(self, session_id: str) -> bool:
        return session_id in self._sessions

    def get(self, session_id: str) -> PlaywrightSession:
        return self._sessions[session_id].session

    def attach(self, session_id: str, chat_id: str) -> PlaywrightSession | None:
        e = self._sessions.get(session_id)
        if not e:
            return None
        e.chats.add(chat_id)
        e.last_used = time.monotonic()
        return e.session

    def detach(self, session_id: str, chat_id: str) -> None:
        e = self._sessions.get(session_id)
        if e:
            e.chats.discard(chat_id)

    # ---- lease --------------------------------------------------------------
    async def acquire(
        self, session_id: str, driver: DriverKind, holder_id: str
    ) -> Lease | None:
        e = self._sessions[session_id]
        async with e.lock:
            cur = e.lease
            if cur.driver != "none" and cur.holder_id != holder_id:
                return None
            e.lease = Lease(driver, holder_id, secrets.token_hex(8))
            (e.agent_may_drive.set if driver == "agent" else e.agent_may_drive.clear)()
            return e.lease

    async def release(self, session_id: str, token: str) -> None:
        e = self._sessions.get(session_id)
        if not e:
            return
        async with e.lock:
            if e.lease.token == token:
                e.lease = Lease("none", "", "")
                e.agent_may_drive.clear()

    def holds(self, session_id: str, token: str) -> bool:
        e = self._sessions.get(session_id)
        return bool(e and e.lease.token == token and token != "")

    def lease_state(self, session_id: str) -> dict:
        e = self._sessions.get(session_id)
        if not e:
            return {"driver": "none", "holder_id": ""}
        return {"driver": e.lease.driver, "holder_id": e.lease.holder_id}

    async def wait_until_agent_may_drive(self, session_id: str) -> None:
        await self._sessions[session_id].agent_may_drive.wait()

    async def reap_idle(self) -> None:
        now = time.monotonic()
        ttl = settings().idle_ttl_seconds
        for sid, e in list(self._sessions.items()):
            # never reap a session that is attached to a chat OR actively being
            # driven (a turn in flight / human takeover holds the lease).
            if not e.chats and e.lease.driver == "none" and now - e.last_used > ttl:
                try:
                    await self._store.save_storage_state(
                        sid, await e.session.storage_state()
                    )
                    await e.session.close()
                except Exception:  # noqa: BLE001
                    pass
                del self._sessions[sid]

    async def shutdown(self) -> None:
        for sid, e in list(self._sessions.items()):
            try:
                await self._store.save_storage_state(
                    sid, await e.session.storage_state()
                )
                await e.session.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
