"""Postgres persistence (asyncpg).

Tables:
  users         — accounts (username + pbkdf2 password hash)
  auth_sessions — login bearer tokens -> user, with expiry
  sessions      — browser sessions: provider + latest storage_state, owned by a user
  chats         — chat -> (browser session, user) binding, with a title
  messages      — PydanticAI message history per chat, serialized with the v2
                  ModelMessagesTypeAdapter so a chat resumes with full context
  steps         — every agent step (action + result + screenshot pointer): the
                  Recorder's sink and the replay trail

A jsonb type codec is registered per connection so we can pass/receive Python
dicts directly for JSONB columns (asyncpg won't implicitly encode dicts).
"""

from __future__ import annotations

import json
from datetime import datetime

import asyncpg
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from .models import StepRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id       TEXT PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token      TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id);
CREATE TABLE IF NOT EXISTS sessions (
  session_id    TEXT PRIMARY KEY,
  user_id       TEXT REFERENCES users(user_id) ON DELETE CASCADE,
  name          TEXT,
  provider      TEXT NOT NULL,
  storage_state JSONB,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE TABLE IF NOT EXISTS chats (
  chat_id    TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
  user_id    TEXT REFERENCES users(user_id) ON DELETE CASCADE,
  title      TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chats_user_idx ON chats(user_id);
CREATE INDEX IF NOT EXISTS chats_session_idx ON chats(session_id);
CREATE TABLE IF NOT EXISTS messages (
  chat_id    TEXT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
  blob       BYTEA NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS steps (
  id          BIGSERIAL PRIMARY KEY,
  chat_id     TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  idx         INT NOT NULL,
  action      JSONB NOT NULL,
  result      JSONB NOT NULL,
  screenshot_uri TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS steps_chat_idx ON steps(chat_id, idx);
"""


async def _init_conn(conn: asyncpg.Connection) -> None:
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(
            typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Store":
        pool = await asyncpg.create_pool(dsn, init=_init_conn)
        async with pool.acquire() as c:
            await c.execute(SCHEMA)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # ---- users --------------------------------------------------------------
    async def create_user(self, user_id: str, username: str, password_hash: str) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO users(user_id, username, password_hash) VALUES($1,$2,$3)",
                user_id, username, password_hash,
            )

    async def get_user_by_username(self, username: str) -> dict | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id, username, password_hash FROM users WHERE username=$1",
                username,
            )
        return dict(row) if row else None

    async def get_user_by_id(self, user_id: str) -> dict | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id, username FROM users WHERE user_id=$1", user_id
            )
        return dict(row) if row else None

    # ---- auth sessions (login tokens) --------------------------------------
    async def create_token(self, token: str, user_id: str, expires_at: datetime) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO auth_sessions(token, user_id, expires_at) VALUES($1,$2,$3)",
                token, user_id, expires_at,
            )

    async def user_for_token(self, token: str) -> str | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id FROM auth_sessions WHERE token=$1 AND expires_at > now()",
                token,
            )
        return row["user_id"] if row else None

    async def delete_token(self, token: str) -> None:
        async with self._pool.acquire() as c:
            await c.execute("DELETE FROM auth_sessions WHERE token=$1", token)

    # ---- browser sessions ---------------------------------------------------
    async def create_session(
        self, session_id: str, user_id: str, name: str, provider: str
    ) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO sessions(session_id, user_id, name, provider) "
                "VALUES($1,$2,$3,$4) ON CONFLICT (session_id) DO NOTHING",
                session_id, user_id, name, provider,
            )

    async def upsert_session(self, session_id: str, provider: str) -> None:
        """Used by the registry when (re)hydrating a session row exists already."""
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO sessions(session_id, provider) VALUES($1,$2) "
                "ON CONFLICT (session_id) DO NOTHING",
                session_id, provider,
            )

    async def get_session(self, session_id: str) -> dict | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT session_id, user_id, name, provider, created_at, updated_at "
                "FROM sessions WHERE session_id=$1",
                session_id,
            )
        return dict(row) if row else None

    async def list_sessions(self, user_id: str) -> list[dict]:
        async with self._pool.acquire() as c:
            rows = await c.fetch(
                "SELECT s.session_id, s.name, s.provider, s.created_at, s.updated_at, "
                "  count(ch.chat_id) AS chat_count "
                "FROM sessions s LEFT JOIN chats ch ON ch.session_id = s.session_id "
                "WHERE s.user_id=$1 "
                "GROUP BY s.session_id ORDER BY s.created_at DESC",
                user_id,
            )
        return [dict(r) for r in rows]

    async def session_owner(self, session_id: str) -> str | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id FROM sessions WHERE session_id=$1", session_id
            )
        return row["user_id"] if row else None

    async def save_storage_state(self, session_id: str, state: dict) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "UPDATE sessions SET storage_state=$2, updated_at=now() "
                "WHERE session_id=$1",
                session_id, state,
            )

    async def load_storage_state(self, session_id: str) -> dict | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT storage_state FROM sessions WHERE session_id=$1", session_id
            )
        return row["storage_state"] if row and row["storage_state"] else None

    # ---- chats --------------------------------------------------------------
    async def create_chat(
        self, chat_id: str, session_id: str, user_id: str, title: str
    ) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO chats(chat_id, session_id, user_id, title) "
                "VALUES($1,$2,$3,$4) ON CONFLICT (chat_id) DO UPDATE "
                "SET session_id=$2, title=$4",
                chat_id, session_id, user_id, title,
            )

    async def get_chat(self, chat_id: str) -> dict | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT chat_id, session_id, user_id, title, created_at "
                "FROM chats WHERE chat_id=$1",
                chat_id,
            )
        return dict(row) if row else None

    async def list_chats(self, user_id: str, session_id: str | None = None) -> list[dict]:
        async with self._pool.acquire() as c:
            if session_id:
                rows = await c.fetch(
                    "SELECT chat_id, session_id, title, created_at FROM chats "
                    "WHERE user_id=$1 AND session_id=$2 ORDER BY created_at DESC",
                    user_id, session_id,
                )
            else:
                rows = await c.fetch(
                    "SELECT chat_id, session_id, title, created_at FROM chats "
                    "WHERE user_id=$1 ORDER BY created_at DESC",
                    user_id,
                )
        return [dict(r) for r in rows]

    async def chat_owner(self, chat_id: str) -> str | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id FROM chats WHERE chat_id=$1", chat_id
            )
        return row["user_id"] if row else None

    async def session_of(self, chat_id: str) -> str | None:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT session_id FROM chats WHERE chat_id=$1", chat_id
            )
        return row["session_id"] if row else None

    # ---- message history (PydanticAI) --------------------------------------
    async def save_messages(self, chat_id: str, messages: list[ModelMessage]) -> None:
        blob = ModelMessagesTypeAdapter.dump_json(messages)   # bytes
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO messages(chat_id, blob) VALUES($1,$2) "
                "ON CONFLICT (chat_id) DO UPDATE SET blob=$2, updated_at=now()",
                chat_id, blob,
            )

    async def load_messages(self, chat_id: str) -> list[ModelMessage]:
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT blob FROM messages WHERE chat_id=$1", chat_id
            )
        if not row:
            return []
        return ModelMessagesTypeAdapter.validate_json(row["blob"])

    async def export_messages(self, chat_id: str) -> list[dict]:
        """Decode message history into a render-friendly transcript for the UI."""
        messages = await self.load_messages(chat_id)
        out: list[dict] = []
        for msg in messages:
            assistant_text: list[str] = []
            for p in getattr(msg, "parts", []):
                kind = getattr(p, "part_kind", "")
                if kind == "user-prompt":
                    content = getattr(p, "content", "")
                    out.append(
                        {"role": "user", "text": content if isinstance(content, str)
                         else str(content)}
                    )
                elif kind == "text":
                    assistant_text.append(getattr(p, "content", ""))
            if assistant_text:
                out.append({"role": "assistant", "text": "".join(assistant_text)})
        return out

    # ---- steps (Recorder sink) ---------------------------------------------
    async def insert_step(self, s: StepRecord) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO steps(chat_id, session_id, idx, action, result, "
                "screenshot_uri) VALUES($1,$2,$3,$4,$5,$6)",
                s.chat_id, s.session_id, s.idx,
                s.action.to_json(),
                {
                    "ok": s.result.ok,
                    "changed": s.result.changed,
                    "error": s.result.error,
                    "observation": s.result.observation.to_json(),
                },
                s.screenshot_uri,
            )

    async def list_steps(self, chat_id: str) -> list[dict]:
        async with self._pool.acquire() as c:
            # order by insertion id (BIGSERIAL) so the replay trail is correct
            # even across turns / any idx reuse
            rows = await c.fetch(
                "SELECT idx, action, result, screenshot_uri, created_at "
                "FROM steps WHERE chat_id=$1 ORDER BY id",
                chat_id,
            )
        return [dict(r) for r in rows]

    async def max_step_idx(self, chat_id: str) -> int:
        """Highest step idx recorded for a chat (0 if none) — lets a new turn
        continue the counter instead of resetting to 0."""
        async with self._pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT COALESCE(MAX(idx), 0) AS m FROM steps WHERE chat_id=$1",
                chat_id,
            )
        return int(row["m"]) if row else 0
