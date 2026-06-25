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


def _ts_col() -> sa.Column:
    return sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now())


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = _TA
    user_id: str = Field(primary_key=True)
    username: str = Field(sa_column=sa.Column(sa.Text, unique=True, nullable=False))
    password_hash: str
    created_at: Optional[datetime] = Field(default=None, sa_column=_ts_col())


class UserApiKey(SQLModel, table=True):
    """A user's BYOK model key (encrypted at rest), one row per provider."""
    __tablename__ = "user_api_keys"
    __table_args__ = _TA
    user_id: str = Field(primary_key=True, foreign_key="users.user_id", ondelete="CASCADE")
    provider: str = Field(primary_key=True)   # "anthropic" | "openai" | "google"
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
        engine = create_async_engine(dsn, pool_pre_ping=True)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            # create_all won't add columns to a table that already exists, so add
            # newer columns explicitly (no-op if already present).
            await conn.execute(
                sa.text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_url TEXT")
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

    # ---- per-user API keys (BYOK, encrypted at rest) -----------------------
    async def save_api_key(self, user_id: str, provider: str, plaintext: str) -> None:
        enc = encrypt(plaintext)
        async with self._sm() as s:
            await s.execute(
                pg_insert(UserApiKey)
                .values(user_id=user_id, provider=provider, encrypted_key=enc)
                .on_conflict_do_update(
                    index_elements=["user_id", "provider"], set_={"encrypted_key": enc}
                )
            )
            await s.commit()

    async def load_user_keys(self, user_id: str) -> dict[str, str]:
        """Decrypted {provider: api_key} for a user (skips any that fail to decrypt)."""
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(UserApiKey.provider, UserApiKey.encrypted_key).where(
                        UserApiKey.user_id == user_id
                    )
                )
            ).all()
        out: dict[str, str] = {}
        for provider, enc in rows:
            pt = decrypt(enc)
            if pt:
                out[provider] = pt
        return out

    async def delete_api_key(self, user_id: str, provider: str) -> None:
        async with self._sm() as s:
            await s.execute(
                sa.delete(UserApiKey).where(
                    UserApiKey.user_id == user_id, UserApiKey.provider == provider
                )
            )
            await s.commit()

    async def list_key_providers(self, user_id: str) -> list[str]:
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(UserApiKey.provider).where(UserApiKey.user_id == user_id)
                )
            ).scalars().all()
        return list(rows)

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
