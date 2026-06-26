"""Postgres persistence via SQLModel (SQLAlchemy async + asyncpg).

Tables (SQLModel models below, same names/columns as before):
  users         — accounts (username + pbkdf2 password hash)
  auth_sessions — login bearer tokens -> user, with expiry
  sessions      — browser sessions: provider + latest storage_state, owned by a user
  chats         — chat -> (browser session, user) binding, with a title
  messages      — PydanticAI message history per chat (ModelMessagesTypeAdapter blob)
  steps         — every agent step (action + result + screenshot pointer)

We use SQLModel models for the tables and `sqlmodel.select` for queries, run on a
SQLAlchemy 2.0 async session (asyncpg driver). JSONB/dict columns round-trip
natively (no manual json codec). The public Store API and its return shapes are
unchanged from the asyncpg version, so the rest of the app is untouched.
"""

from __future__ import annotations

import json
import ssl
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import Field, SQLModel, select
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from .crypto import decrypt, encrypt
from .models import StepRecord

# extend_existing so importing this module twice (e.g. uvicorn --reload) doesn't
# raise "Table already defined" against SQLModel's shared metadata.
_TA = {"extend_existing": True}

# Browserbase BYOK creds (api_key + project_id) ride in the same session_api_keys
# table as the model keys, under this pseudo-provider, as an encrypted JSON blob.
_BROWSERBASE_PROVIDER = "browserbase"


def _ts_col() -> sa.Column:
    return sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now())


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = _TA
    user_id: str = Field(primary_key=True)
    username: str = Field(sa_column=sa.Column(sa.Text, unique=True, nullable=False))
    password_hash: str
    created_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())


class SessionApiKey(SQLModel, table=True):
    """BYOK keys scoped to a browser session (encrypted at rest), one row per
    provider. Web users bring their own keys per session; the rows are purged
    when the session is reaped (ON DELETE CASCADE covers an explicit session
    delete; reap_idle deletes them explicitly since reaping keeps the row)."""
    __tablename__ = "session_api_keys"
    __table_args__ = _TA
    session_id: str = Field(primary_key=True, foreign_key="sessions.session_id", ondelete="CASCADE")
    provider: str = Field(primary_key=True)   # anthropic | openai | google | browserbase
    encrypted_key: str
    created_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())


class AuthSession(SQLModel, table=True):
    __tablename__ = "auth_sessions"
    __table_args__ = _TA
    token: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="users.user_id", index=True, ondelete="CASCADE")
    created_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())
    # tz-aware to match the TIMESTAMPTZ column (and the tz-aware expiry we pass)
    expires_at: datetime = Field(sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False))


class BrowserSession(SQLModel, table=True):
    __tablename__ = "sessions"
    __table_args__ = _TA
    session_id: str = Field(primary_key=True)
    user_id: Optional[str] = Field(default=None, foreign_key="users.user_id", index=True, ondelete="CASCADE")
    name: Optional[str] = None
    provider: str
    storage_state: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB))
    last_url: Optional[str] = None   # primary tab's URL, restored on rehydrate
    # browserbase session id — lets any replica/restart reconnect to the same live
    # browser instead of orphaning it (see registry.create / BrowserbaseProvider).
    bb_session_id: Optional[str] = Field(default=None, index=True)
    created_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())
    updated_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())


class Chat(SQLModel, table=True):
    __tablename__ = "chats"
    __table_args__ = _TA
    chat_id: str = Field(primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.session_id", index=True, ondelete="CASCADE")
    user_id: Optional[str] = Field(default=None, foreign_key="users.user_id", index=True, ondelete="CASCADE")
    title: Optional[str] = None
    created_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = _TA
    chat_id: str = Field(primary_key=True, foreign_key="chats.chat_id", ondelete="CASCADE")
    blob: bytes = Field(sa_column=sa.Column(sa.LargeBinary, nullable=False))
    updated_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())


class Step(SQLModel, table=True):
    __tablename__ = "steps"
    __table_args__ = _TA
    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(index=True)
    session_id: str
    idx: int
    action: dict = Field(sa_column=sa.Column(JSONB, nullable=False))
    result: dict = Field(sa_column=sa.Column(JSONB, nullable=False))
    screenshot_uri: Optional[str] = None
    created_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())


def _content_text(content) -> str:
    """Best-effort text from a message-part content (str or list of items)."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(c for c in content if isinstance(c, str))
    return ""


def _call_args(p) -> object:
    """Tool-call args as a dict when possible, else the raw value."""
    try:
        return p.args_as_dict()
    except Exception:  # noqa: BLE001
        return getattr(p, "args", None)


def _return_summary(p) -> dict:
    """Compact, render-friendly summary of a tool-return part (no raw bytes)."""
    content = getattr(p, "content", "")
    text = _content_text(content)
    if not text:
        try:
            text = p.model_response_str()
        except Exception:  # noqa: BLE001
            text = ""
    ok = not text.lstrip().lower().startswith("[error")
    return {"ok": ok, "text": text[:600]}


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    async def connect(cls, dsn: str) -> "Store":
        # SQLAlchemy async needs the asyncpg driver explicit in the URL. Accept
        # both postgresql:// and the postgres:// alias many providers hand out.
        if dsn.startswith("postgresql://"):
            dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif dsn.startswith("postgres://"):
            dsn = dsn.replace("postgres://", "postgresql+asyncpg://", 1)
        # asyncpg caches prepared statements per connection; Supabase/pgbouncer
        # poolers reuse backends, which breaks cached plans — disable the cache.
        connect_args: dict = {"statement_cache_size": 0}
        # Managed Postgres (Supabase etc.) requires TLS; attach an SSL context for
        # remote hosts but skip it for the local docker-compose Postgres.
        if not any(h in dsn for h in ("@localhost", "@127.0.0.1")):
            connect_args["ssl"] = ssl.create_default_context()
        engine = create_async_engine(dsn, pool_pre_ping=True, connect_args=connect_args)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            # create_all won't add columns to a table that already exists, so add
            # newer columns explicitly (no-op if already present).
            await conn.execute(
                sa.text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_url TEXT")
            )
            await conn.execute(
                sa.text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS bb_session_id TEXT")
            )
        return cls(engine)

    async def close(self) -> None:
        await self._engine.dispose()

    # ---- users --------------------------------------------------------------
    async def create_user(self, user_id: str, username: str, password_hash: str) -> None:
        async with self._sm() as s:
            s.add(User(user_id=user_id, username=username, password_hash=password_hash))
            await s.commit()

    async def get_user_by_username(self, username: str) -> dict | None:
        async with self._sm() as s:
            u = (await s.execute(select(User).where(User.username == username))).scalars().first()
        return (
            {"user_id": u.user_id, "username": u.username, "password_hash": u.password_hash}
            if u else None
        )

    async def get_user_by_id(self, user_id: str) -> dict | None:
        async with self._sm() as s:
            u = (await s.execute(select(User).where(User.user_id == user_id))).scalars().first()
        return {"user_id": u.user_id, "username": u.username} if u else None

    # ---- per-SESSION BYOK keys (encrypted at rest; purged when session reaped) ---
    async def save_session_key(self, session_id: str, provider: str, plaintext: str) -> None:
        enc = encrypt(plaintext)
        async with self._sm() as s:
            await s.execute(
                pg_insert(SessionApiKey)
                .values(session_id=session_id, provider=provider, encrypted_key=enc)
                .on_conflict_do_update(
                    index_elements=["session_id", "provider"], set_={"encrypted_key": enc}
                )
            )
            await s.commit()

    async def load_session_keys(self, session_id: str) -> dict[str, str]:
        """Decrypted {provider: api_key} of a session's MODEL keys (excludes the
        2-field browserbase blob; skips any that fail to decrypt)."""
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(SessionApiKey.provider, SessionApiKey.encrypted_key).where(
                        SessionApiKey.session_id == session_id,
                        SessionApiKey.provider != _BROWSERBASE_PROVIDER,
                    )
                )
            ).all()
        out: dict[str, str] = {}
        for provider, enc in rows:
            pt = decrypt(enc)
            if pt:
                out[provider] = pt
        return out

    async def delete_session_key(self, session_id: str, provider: str) -> None:
        async with self._sm() as s:
            await s.execute(
                sa.delete(SessionApiKey).where(
                    SessionApiKey.session_id == session_id,
                    SessionApiKey.provider == provider,
                )
            )
            await s.commit()

    async def delete_session_keys(self, session_id: str) -> None:
        """Purge ALL of a session's keys (called when the session is reaped)."""
        async with self._sm() as s:
            await s.execute(
                sa.delete(SessionApiKey).where(SessionApiKey.session_id == session_id)
            )
            await s.commit()

    async def list_session_key_providers(self, session_id: str) -> list[str]:
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(SessionApiKey.provider).where(
                        SessionApiKey.session_id == session_id
                    )
                )
            ).scalars().all()
        return list(rows)

    async def save_session_browserbase_creds(
        self, session_id: str, api_key: str, project_id: str
    ) -> None:
        await self.save_session_key(
            session_id, _BROWSERBASE_PROVIDER,
            json.dumps({"api_key": api_key, "project_id": project_id}),
        )

    async def load_session_browserbase_creds(self, session_id: str) -> dict | None:
        """{'api_key','project_id'} for a session, or None if unset/undecryptable."""
        async with self._sm() as s:
            enc = (
                await s.execute(
                    select(SessionApiKey.encrypted_key).where(
                        SessionApiKey.session_id == session_id,
                        SessionApiKey.provider == _BROWSERBASE_PROVIDER,
                    )
                )
            ).scalars().first()
        if not enc:
            return None
        pt = decrypt(enc)
        if not pt:
            return None
        try:
            d = json.loads(pt)
        except Exception:  # noqa: BLE001
            return None
        if d.get("api_key") and d.get("project_id"):
            return {"api_key": d["api_key"], "project_id": d["project_id"]}
        return None

    # ---- auth sessions (login tokens) --------------------------------------
    async def create_token(self, token: str, user_id: str, expires_at: datetime) -> None:
        async with self._sm() as s:
            s.add(AuthSession(token=token, user_id=user_id, expires_at=expires_at))
            await s.commit()

    async def user_for_token(self, token: str) -> str | None:
        async with self._sm() as s:
            row = (
                await s.execute(
                    select(AuthSession).where(
                        AuthSession.token == token,
                        AuthSession.expires_at > sa.func.now(),
                    )
                )
            ).scalars().first()
        return row.user_id if row else None

    async def delete_token(self, token: str) -> None:
        async with self._sm() as s:
            await s.execute(sa.delete(AuthSession).where(AuthSession.token == token))
            await s.commit()

    # ---- browser sessions ---------------------------------------------------
    async def create_session(
        self, session_id: str, user_id: str, name: str, provider: str
    ) -> None:
        async with self._sm() as s:
            await s.execute(
                pg_insert(BrowserSession)
                .values(session_id=session_id, user_id=user_id, name=name, provider=provider)
                .on_conflict_do_nothing(index_elements=["session_id"])
            )
            await s.commit()

    async def upsert_session(self, session_id: str, provider: str) -> None:
        async with self._sm() as s:
            await s.execute(
                pg_insert(BrowserSession)
                .values(session_id=session_id, provider=provider)
                .on_conflict_do_nothing(index_elements=["session_id"])
            )
            await s.commit()

    async def get_session(self, session_id: str) -> dict | None:
        async with self._sm() as s:
            r = (
                await s.execute(select(BrowserSession).where(BrowserSession.session_id == session_id))
            ).scalars().first()
        if not r:
            return None
        return {
            "session_id": r.session_id, "user_id": r.user_id, "name": r.name,
            "provider": r.provider, "created_at": r.created_at, "updated_at": r.updated_at,
            "bb_session_id": r.bb_session_id,
        }

    async def list_sessions(self, user_id: str) -> list[dict]:
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(BrowserSession)
                    .where(BrowserSession.user_id == user_id)
                    .order_by(sa.desc(BrowserSession.created_at))
                )
            ).scalars().all()
            counts = dict(
                (
                    await s.execute(
                        select(Chat.session_id, sa.func.count())
                        .where(Chat.user_id == user_id)
                        .group_by(Chat.session_id)
                    )
                ).all()
            )
        return [
            {
                "session_id": r.session_id, "name": r.name, "provider": r.provider,
                "created_at": r.created_at, "updated_at": r.updated_at,
                "chat_count": int(counts.get(r.session_id, 0)),
            }
            for r in rows
        ]

    async def session_owner(self, session_id: str) -> str | None:
        async with self._sm() as s:
            r = (
                await s.execute(
                    select(BrowserSession.user_id).where(BrowserSession.session_id == session_id)
                )
            ).scalars().first()
        return r if r else None

    async def save_storage_state(
        self, session_id: str, state: dict, last_url: str | None = None
    ) -> None:
        values: dict = {"storage_state": state, "updated_at": sa.func.now()}
        if last_url is not None:
            values["last_url"] = last_url
        async with self._sm() as s:
            await s.execute(
                sa.update(BrowserSession)
                .where(BrowserSession.session_id == session_id)
                .values(**values)
            )
            await s.commit()

    async def load_storage_state(self, session_id: str) -> dict | None:
        async with self._sm() as s:
            r = (
                await s.execute(
                    select(BrowserSession.storage_state).where(
                        BrowserSession.session_id == session_id
                    )
                )
            ).scalars().first()
        return r if r else None

    async def load_last_url(self, session_id: str) -> str | None:
        async with self._sm() as s:
            r = (
                await s.execute(
                    select(BrowserSession.last_url).where(
                        BrowserSession.session_id == session_id
                    )
                )
            ).scalars().first()
        return r or None

    async def save_bb_session_id(self, session_id: str, bb_session_id: str | None) -> None:
        async with self._sm() as s:
            await s.execute(
                sa.update(BrowserSession)
                .where(BrowserSession.session_id == session_id)
                .values(bb_session_id=bb_session_id)
            )
            await s.commit()

    async def load_bb_session_id(self, session_id: str) -> str | None:
        async with self._sm() as s:
            r = (
                await s.execute(
                    select(BrowserSession.bb_session_id).where(
                        BrowserSession.session_id == session_id
                    )
                )
            ).scalars().first()
        return r or None

    # ---- chats --------------------------------------------------------------
    async def create_chat(
        self, chat_id: str, session_id: str, user_id: str, title: str
    ) -> None:
        async with self._sm() as s:
            await s.execute(
                pg_insert(Chat)
                .values(chat_id=chat_id, session_id=session_id, user_id=user_id, title=title)
                .on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={"session_id": session_id, "title": title},
                )
            )
            await s.commit()

    async def get_chat(self, chat_id: str) -> dict | None:
        async with self._sm() as s:
            r = (await s.execute(select(Chat).where(Chat.chat_id == chat_id))).scalars().first()
        if not r:
            return None
        return {
            "chat_id": r.chat_id, "session_id": r.session_id, "user_id": r.user_id,
            "title": r.title, "created_at": r.created_at,
        }

    async def list_chats(self, user_id: str, session_id: str | None = None) -> list[dict]:
        stmt = select(Chat).where(Chat.user_id == user_id)
        if session_id:
            stmt = stmt.where(Chat.session_id == session_id)
        stmt = stmt.order_by(sa.desc(Chat.created_at))
        async with self._sm() as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [
            {"chat_id": r.chat_id, "session_id": r.session_id, "title": r.title,
             "created_at": r.created_at}
            for r in rows
        ]

    async def chat_owner(self, chat_id: str) -> str | None:
        async with self._sm() as s:
            r = (await s.execute(select(Chat.user_id).where(Chat.chat_id == chat_id))).scalars().first()
        return r if r else None

    async def session_of(self, chat_id: str) -> str | None:
        async with self._sm() as s:
            r = (await s.execute(select(Chat.session_id).where(Chat.chat_id == chat_id))).scalars().first()
        return r if r else None

    # ---- message history (PydanticAI) --------------------------------------
    async def save_messages(self, chat_id: str, messages: list[ModelMessage]) -> None:
        blob = ModelMessagesTypeAdapter.dump_json(messages)   # bytes
        async with self._sm() as s:
            await s.execute(
                pg_insert(Message)
                .values(chat_id=chat_id, blob=blob)
                .on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={"blob": blob, "updated_at": sa.func.now()},
                )
            )
            await s.commit()

    async def load_messages(self, chat_id: str) -> list[ModelMessage]:
        async with self._sm() as s:
            r = (await s.execute(select(Message.blob).where(Message.chat_id == chat_id))).scalars().first()
        if not r:
            return []
        return ModelMessagesTypeAdapter.validate_json(bytes(r))

    async def export_messages(self, chat_id: str) -> list[dict]:
        """Decode message history into a render-friendly transcript for the UI.

        Each assistant message carries an ordered `items` trail reconstructed from
        the model-message parts (thinking / text / tool_call), with each
        tool_call's `result` filled from its matching tool-return. This is the
        SAME item shape the live WS stream produces, so live and reloaded turns
        render identically. Screenshots stay in the separate steps trail."""
        messages = await self.load_messages(chat_id)
        out: list[dict] = []
        call_index: dict[str, dict] = {}   # tool_call_id -> its tool_call item
        turn: list[dict] = []              # items accumulated for the current turn

        def flush() -> None:
            if turn:
                final_text = "".join(i["text"] for i in turn if i["kind"] == "text")
                out.append({"role": "assistant", "text": final_text, "items": list(turn)})
                turn.clear()

        for msg in messages:
            mkind = getattr(msg, "kind", "")
            parts = getattr(msg, "parts", [])
            if mkind == "request":
                for p in parts:
                    pk = getattr(p, "part_kind", "")
                    if pk == "user-prompt":
                        text = _content_text(getattr(p, "content", ""))
                        # skip image-only user-prompts (vision tool-result attachments)
                        if text.strip():
                            flush()  # a new user turn closes the prior assistant turn
                            out.append({"role": "user", "text": text})
                    elif pk == "tool-return":
                        item = call_index.get(getattr(p, "tool_call_id", ""))
                        if item is not None:
                            item["result"] = _return_summary(p)
            elif mkind == "response":
                for p in parts:
                    pk = getattr(p, "part_kind", "")
                    if pk == "thinking":
                        turn.append({"kind": "thinking", "text": getattr(p, "content", "")})
                    elif pk == "text":
                        turn.append({"kind": "text", "text": getattr(p, "content", "")})
                    elif pk == "tool-call":
                        it = {
                            "kind": "tool_call",
                            "tool": getattr(p, "tool_name", ""),
                            "args": _call_args(p),
                            "tool_call_id": getattr(p, "tool_call_id", ""),
                            "result": None,
                        }
                        turn.append(it)
                        call_index[it["tool_call_id"]] = it
        flush()  # trailing assistant turn
        return out

    # ---- steps (Recorder sink) ---------------------------------------------
    async def insert_step(self, rec: StepRecord) -> None:
        async with self._sm() as s:
            s.add(
                Step(
                    chat_id=rec.chat_id,
                    session_id=rec.session_id,
                    idx=rec.idx,
                    action=rec.action.to_json(),
                    result={
                        "ok": rec.result.ok,
                        "changed": rec.result.changed,
                        "error": rec.result.error,
                        "observation": rec.result.observation.to_json(),
                    },
                    screenshot_uri=rec.screenshot_uri,
                )
            )
            await s.commit()

    async def list_steps(self, chat_id: str) -> list[dict]:
        async with self._sm() as s:
            # insertion order (id) so the replay trail is correct across turns
            rows = (
                await s.execute(
                    select(Step).where(Step.chat_id == chat_id).order_by(sa.asc(Step.id))
                )
            ).scalars().all()
        return [
            {"idx": r.idx, "action": r.action, "result": r.result,
             "screenshot_uri": r.screenshot_uri, "created_at": r.created_at}
            for r in rows
        ]

    async def max_step_idx(self, chat_id: str) -> int:
        async with self._sm() as s:
            r = (
                await s.execute(
                    select(sa.func.coalesce(sa.func.max(Step.idx), 0)).where(
                        Step.chat_id == chat_id
                    )
                )
            ).scalar()
        return int(r) if r is not None else 0
