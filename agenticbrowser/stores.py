# SPDX-License-Identifier: Apache-2.0
"""The `Store` Protocol the agent CORE depends on, plus zero-server impls.

The core (registry / runner / recorder) only ever calls the 12 methods below —
verified by grep over those modules. By depending on this narrow Protocol instead
of the concrete SQLModel/Postgres class, the agent runs headless with no database.
Keys are never a store concern — they live in `CoreConfig`.

Implementations here:
  • MemoryStore — everything in RAM. Ephemeral: lost on process exit.
  • SqliteStore — same surface persisted to a local SQLite file (WAL), so a single
                  process gets resumable sessions + a replay trail with no Postgres.

The server's full SQLModel/Postgres class (with all the extra user/auth/chat
methods) lives in `agenticbrowser/server/store_sql.py` and also satisfies this
Protocol structurally.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from .models import StepRecord


@runtime_checkable
class Store(Protocol):
    """The 12 methods the agent core actually calls.

    Keys are NOT a Store concern: provider API keys + Browserbase creds come from
    `CoreConfig` (the SDK's `keys=`/`browserbase=`, or the server's .env), never from
    the store. The store only persists browser/session/conversation state.
    """

    # --- browser session state (registry) ---
    async def load_storage_state(self, session_id: str) -> dict | None: ...
    async def save_storage_state(
        self, session_id: str, storage_state: dict, last_url: str | None = None
    ) -> None: ...
    async def load_last_url(self, session_id: str) -> str | None: ...
    async def load_bb_session_id(self, session_id: str) -> str | None: ...
    async def save_bb_session_id(self, session_id: str, bb_session_id: str | None) -> None: ...
    async def upsert_session(self, session_id: str, provider: str) -> None: ...
    async def get_session(self, session_id: str) -> dict | None: ...
    # --- conversation (runner) ---
    async def load_messages(self, chat_id: str) -> list[ModelMessage]: ...
    async def save_messages(self, chat_id: str, messages: list[ModelMessage]) -> None: ...
    # one entry per assistant turn, appended in order and saved with the
    # conversation so per-message + session usage survive a reload.
    async def append_turn_usage(self, chat_id: str, usage: dict) -> None: ...
    async def max_step_idx(self, chat_id: str) -> int: ...
    # --- replay trail (recorder) ---
    async def insert_step(self, rec: StepRecord) -> None: ...


# --------------------------------------------------------------------------- #
# In-memory
# --------------------------------------------------------------------------- #
class MemoryStore:
    """Ephemeral, dependency-free Store. Keys live in CoreConfig, not here."""

    def __init__(self) -> None:
        self._storage: dict[str, dict] = {}
        self._last_url: dict[str, str] = {}
        self._bb_id: dict[str, str | None] = {}
        self._sessions: dict[str, dict] = {}
        self._messages: dict[str, list[ModelMessage]] = {}
        self._usage: dict[str, list[dict]] = {}
        self._steps: dict[str, list[StepRecord]] = {}

    async def load_storage_state(self, session_id):
        return self._storage.get(session_id)

    async def save_storage_state(self, session_id, storage_state, last_url=None):
        self._storage[session_id] = storage_state
        if last_url is not None:
            self._last_url[session_id] = last_url

    async def load_last_url(self, session_id):
        return self._last_url.get(session_id)

    async def load_bb_session_id(self, session_id):
        return self._bb_id.get(session_id)

    async def save_bb_session_id(self, session_id, bb_session_id):
        self._bb_id[session_id] = bb_session_id

    async def upsert_session(self, session_id, provider):
        self._sessions[session_id] = {"session_id": session_id, "provider": provider}

    async def get_session(self, session_id):
        return self._sessions.get(session_id)

    async def load_messages(self, chat_id):
        return list(self._messages.get(chat_id, []))

    async def save_messages(self, chat_id, messages):
        self._messages[chat_id] = list(messages)

    async def append_turn_usage(self, chat_id, usage):
        self._usage.setdefault(chat_id, []).append(dict(usage))

    async def load_turn_usage(self, chat_id) -> list[dict]:
        return list(self._usage.get(chat_id, []))

    async def max_step_idx(self, chat_id):
        return max((s.idx for s in self._steps.get(chat_id, [])), default=0)

    async def insert_step(self, rec: StepRecord):
        self._steps.setdefault(rec.chat_id, []).append(rec)

    # convenience for tests / embeds (not part of the Protocol)
    async def list_steps(self, chat_id, since: int = 0) -> list[dict]:
        return [_step_to_dict(s) for s in self._steps.get(chat_id, []) if s.idx > since]


# --------------------------------------------------------------------------- #
# SQLite (WAL) — single-process persistence, no Postgres
# --------------------------------------------------------------------------- #
def _step_dict(idx, action, ok, changed, url, screenshot_uri) -> dict:
    """The one canonical step-row shape (shared by both stores' list_steps)."""
    return {
        "idx": idx, "action": action, "ok": ok,
        "changed": changed, "url": url, "screenshot_uri": screenshot_uri,
    }


def _step_to_dict(rec: StepRecord) -> dict:
    return _step_dict(
        rec.idx, rec.action.to_json(), rec.result.ok, rec.result.changed,
        rec.result.observation.url, rec.screenshot_uri,
    )


class SqliteStore:
    """Persisted Store backed by a local SQLite file. Keys live in CoreConfig."""

    def __init__(self, path: str = "agenticbrowser.db") -> None:
        self._path = path
        self._conn = None  # aiosqlite.Connection, opened lazily

    async def _db(self):
        if self._conn is None:
            import aiosqlite  # lazy: keeps `import agenticbrowser` free of aiosqlite

            self._conn = await aiosqlite.connect(self._path)
            await self._conn.execute("PRAGMA journal_mode=WAL")
            # single-process embed persistence: NORMAL is safe under WAL and avoids
            # an fsync on every per-step commit (only checkpoints fsync).
            await self._conn.execute("PRAGMA synchronous=NORMAL")
            await self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                  session_id TEXT PRIMARY KEY, provider TEXT,
                  storage_state TEXT, last_url TEXT, bb_session_id TEXT);
                CREATE TABLE IF NOT EXISTS messages (
                  chat_id TEXT PRIMARY KEY, blob BLOB,
                  usage TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS steps (
                  chat_id TEXT, idx INTEGER, action TEXT, ok INTEGER,
                  changed INTEGER, url TEXT, screenshot_uri TEXT,
                  PRIMARY KEY (chat_id, idx));
                """
            )
            # add `usage` to message tables created before it existed (CREATE IF
            # NOT EXISTS above can't alter an existing table). No-op if present.
            try:
                await self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN usage TEXT NOT NULL DEFAULT '[]'"
                )
            except Exception:  # noqa: BLE001 — column already exists
                pass
            await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _scalar(self, sql, args=()):
        db = await self._db()
        async with db.execute(sql, args) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def load_storage_state(self, session_id):
        raw = await self._scalar(
            "SELECT storage_state FROM sessions WHERE session_id=?", (session_id,)
        )
        return json.loads(raw) if raw else None

    async def save_storage_state(self, session_id, storage_state, last_url=None):
        db = await self._db()
        await db.execute(
            """INSERT INTO sessions (session_id, storage_state, last_url)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 storage_state=excluded.storage_state,
                 last_url=COALESCE(excluded.last_url, sessions.last_url)""",
            (session_id, json.dumps(storage_state), last_url),
        )
        await db.commit()

    async def load_last_url(self, session_id):
        return await self._scalar(
            "SELECT last_url FROM sessions WHERE session_id=?", (session_id,)
        )

    async def load_bb_session_id(self, session_id):
        return await self._scalar(
            "SELECT bb_session_id FROM sessions WHERE session_id=?", (session_id,)
        )

    async def save_bb_session_id(self, session_id, bb_session_id):
        db = await self._db()
        await db.execute(
            """INSERT INTO sessions (session_id, bb_session_id) VALUES (?, ?)
               ON CONFLICT(session_id) DO UPDATE SET bb_session_id=excluded.bb_session_id""",
            (session_id, bb_session_id),
        )
        await db.commit()

    async def upsert_session(self, session_id, provider):
        db = await self._db()
        await db.execute(
            """INSERT INTO sessions (session_id, provider) VALUES (?, ?)
               ON CONFLICT(session_id) DO UPDATE SET provider=excluded.provider""",
            (session_id, provider),
        )
        await db.commit()

    async def get_session(self, session_id):
        prov = await self._scalar(
            "SELECT provider FROM sessions WHERE session_id=?", (session_id,)
        )
        return None if prov is None else {"session_id": session_id, "provider": prov}

    async def load_messages(self, chat_id):
        raw = await self._scalar("SELECT blob FROM messages WHERE chat_id=?", (chat_id,))
        return list(ModelMessagesTypeAdapter.validate_json(raw)) if raw else []

    async def save_messages(self, chat_id, messages):
        db = await self._db()
        blob = ModelMessagesTypeAdapter.dump_json(messages)
        # leave `usage` untouched on conflict (it accumulates across turns); a new
        # row defaults it to '[]'.
        await db.execute(
            """INSERT INTO messages (chat_id, blob) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET blob=excluded.blob""",
            (chat_id, blob),
        )
        await db.commit()

    async def append_turn_usage(self, chat_id, usage):
        db = await self._db()
        # save_messages always runs first, so the row exists; append to its list.
        raw = await self._scalar("SELECT usage FROM messages WHERE chat_id=?", (chat_id,))
        lst = json.loads(raw) if raw else []
        lst.append(dict(usage))
        await db.execute(
            "UPDATE messages SET usage=? WHERE chat_id=?", (json.dumps(lst), chat_id)
        )
        await db.commit()

    async def load_turn_usage(self, chat_id) -> list[dict]:
        raw = await self._scalar("SELECT usage FROM messages WHERE chat_id=?", (chat_id,))
        return json.loads(raw) if raw else []

    async def max_step_idx(self, chat_id):
        v = await self._scalar("SELECT MAX(idx) FROM steps WHERE chat_id=?", (chat_id,))
        return int(v) if v is not None else 0

    async def insert_step(self, rec: StepRecord):
        db = await self._db()
        await db.execute(
            """INSERT OR REPLACE INTO steps
               (chat_id, idx, action, ok, changed, url, screenshot_uri)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                rec.chat_id, rec.idx, json.dumps(rec.action.to_json()),
                int(rec.result.ok), int(rec.result.changed),
                rec.result.observation.url, rec.screenshot_uri,
            ),
        )
        await db.commit()

    async def list_steps(self, chat_id, since: int = 0) -> list[dict]:
        db = await self._db()
        async with db.execute(
            "SELECT idx, action, ok, changed, url, screenshot_uri "
            "FROM steps WHERE chat_id=? AND idx>? ORDER BY idx",
            (chat_id, since),
        ) as cur:
            rows = await cur.fetchall()
        return [
            _step_dict(r[0], json.loads(r[1]), bool(r[2]), bool(r[3]), r[4], r[5])
            for r in rows
        ]
