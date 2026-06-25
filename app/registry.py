"""SessionRegistry — named, long-lived browser sessions + per-tab driver leases.

Sessions outlive chats. On create we restore persisted storage_state so auth
survives restarts. `ensure()` lazily re-opens a session that exists in the DB
but isn't live in memory (e.g. after a server restart).

Leases are **per tab**: the orchestrator drives the primary tab `t0` while
sub-agents each drive their own tab (`t1`, `t2`, …) concurrently, and a human can
take over any single tab. Each tab has its own `agent_may_drive` event so taking
over one tab blocks only that tab's agent, not the others. Methods default to the
primary tab `t0`, so existing single-tab callers are unaffected.
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

_PRIMARY = "t0"


@dataclass
class _Entry:
    session: PlaywrightSession
    provider: ProviderName
    # per-tab lease + "agent may drive" gate (lazily created per tab_id)
    leases: dict[str, Lease] = field(default_factory=dict)
    may_drive: dict[str, asyncio.Event] = field(default_factory=dict)
    chats: set[str] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _ld(self, tab_id: str) -> tuple[Lease, asyncio.Event]:
        if tab_id not in self.leases:
            self.leases[tab_id] = Lease("none", "", "")
            self.may_drive[tab_id] = asyncio.Event()
        return self.leases[tab_id], self.may_drive[tab_id]


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
            prior = await self._store.load_storage_state(session_id)
            session = await PlaywrightSession.open(provider, storage_state=prior)
            # restore the last page so the browser comes back where it stopped
            # (cookies/localStorage are already restored via storage_state).
            last_url = await self._store.load_last_url(session_id)
            if last_url and last_url.startswith(("http://", "https://")):
                await session.goto(last_url)
            await self._store.upsert_session(session_id, provider.name)
            self._sessions[session_id] = _Entry(
                session=session, provider=provider.name
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

    # ---- lease (per tab) ----------------------------------------------------
    async def acquire(
        self, session_id: str, driver: DriverKind, holder_id: str, tab_id: str = _PRIMARY
    ) -> Lease | None:
        e = self._sessions[session_id]
        async with e.lock:
            cur, ev = e._ld(tab_id)
            if cur.driver != "none" and cur.holder_id != holder_id:
                # A human may take over a tab the agent is driving (that's the
                # whole point of "take control"); the agent must wait for a human.
                if not (driver == "human" and cur.driver == "agent"):
                    return None
            lease = Lease(driver, holder_id, secrets.token_hex(8))
            e.leases[tab_id] = lease
            # the agent may drive whenever a human is NOT holding the tab
            (ev.clear if driver == "human" else ev.set)()
            return lease

    async def release(
        self, session_id: str, token: str, tab_id: str | None = None
    ) -> None:
        e = self._sessions.get(session_id)
        if not e:
            return
        async with e.lock:
            # release by token: find the tab holding it (so callers that only
            # have the token, like the runner, keep working without a tab_id).
            targets = [tab_id] if tab_id is not None else list(e.leases)
            for tid in targets:
                lease = e.leases.get(tid)
                if lease and lease.token == token and token != "":
                    e.leases[tid] = Lease("none", "", "")
                    # freeing the tab lets the agent resume (e.g. a human who took
                    # over mid-turn handing control back to a still-running agent)
                    e.may_drive.setdefault(tid, asyncio.Event()).set()
                    return

    def holds(self, session_id: str, token: str, tab_id: str = _PRIMARY) -> bool:
        e = self._sessions.get(session_id)
        if not e:
            return False
        lease = e.leases.get(tab_id)
        return bool(lease and lease.token == token and token != "")

    def lease_state(self, session_id: str, tab_id: str = _PRIMARY) -> dict:
        e = self._sessions.get(session_id)
        if not e or tab_id not in e.leases:
            return {"driver": "none", "holder_id": "", "tab_id": tab_id}
        lease = e.leases[tab_id]
        return {"driver": lease.driver, "holder_id": lease.holder_id, "tab_id": tab_id}

    def lease_states(self, session_id: str) -> list[dict]:
        e = self._sessions.get(session_id)
        if not e:
            return []
        return [
            {"tab_id": tid, "driver": l.driver, "holder_id": l.holder_id}
            for tid, l in e.leases.items()
        ]

    async def wait_until_agent_may_drive(
        self, session_id: str, tab_id: str = _PRIMARY
    ) -> None:
        e = self._sessions[session_id]
        _, ev = e._ld(tab_id)
        await ev.wait()

    async def reap_idle(self) -> None:
        now = time.monotonic()
        ttl = settings().idle_ttl_seconds
        for sid, e in list(self._sessions.items()):
            # never reap a session attached to a chat OR with ANY tab being driven
            # (a turn in flight / human takeover holds a tab lease).
            any_driven = any(l.driver != "none" for l in e.leases.values())
            if not e.chats and not any_driven and now - e.last_used > ttl:
                try:
                    await self._store.save_storage_state(
                        sid, await e.session.storage_state(), last_url=e.session.url
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
