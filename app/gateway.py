"""Gateway — FastAPI. Hub for the React (Vite) app.

REST (all under /api, bearer-auth except register/login):
  POST /api/auth/register|login         -> {token, user}
  GET  /api/auth/me                      -> current user
  POST /api/auth/logout                  -> invalidate token
  POST /api/sessions                     -> create a browser session (opens browser)
  GET  /api/sessions                     -> list my browser sessions
  GET  /api/sessions/{sid}               -> session detail + live state
  POST /api/chats                        -> create a chat bound to a session
  GET  /api/chats[?session_id=]          -> list my chats
  GET  /api/chats/{cid}/messages         -> transcript (resume)
  GET  /api/chats/{cid}/steps            -> step replay trail

WebSockets (token via ?token=):
  /ws/chat/{chat_id}     streams tokens + steps + approval requests; takes user
                         messages and approval decisions
  /ws/view/{session_id}  announces live-view mode; screencast frames + input
                         injection (local) or an iframe URL (browserbase); the
                         lease gates the agent on take_over/release.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import (
    _bearer,
    current_user,
    hash_password,
    new_token,
    new_user_id,
    token_expiry,
    user_for_ws,
    verify_password,
)
from .config import ROOT, settings
from .models import ProviderName, StreamEvent
from .recorder import LocalArtifacts, Recorder
from .registry import SessionRegistry
from .runner import Runner
from .store import Store


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = await Store.connect(settings().database_url)
    registry = SessionRegistry(store)
    recorder = Recorder(store, LocalArtifacts(settings().artifacts_dir))
    runner = Runner(registry, store, recorder)
    app.state.store = store
    app.state.registry = registry
    app.state.recorder = recorder
    app.state.runner = runner
    app.state.tasks = set()   # keep refs to fire-and-forget run_turn tasks

    async def reaper():
        while True:
            await asyncio.sleep(300)
            await registry.reap_idle()

    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        await registry.shutdown()
        await store.close()


app = FastAPI(title="Agentic Browser", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# screenshots / replay artifacts are served through an ownership-checked route
# (see /api/artifacts/{chat_id}/{filename}), NOT a public static mount.
Path(settings().artifacts_dir).mkdir(parents=True, exist_ok=True)


# --- request bodies ----------------------------------------------------------
class Credentials(BaseModel):
    username: str
    password: str


class CreateSession(BaseModel):
    name: str | None = None
    provider: ProviderName | None = None


class CreateChat(BaseModel):
    session_id: str
    title: str | None = None


# --- auth --------------------------------------------------------------------
@app.post("/api/auth/register")
async def register(body: Credentials):
    store: Store = app.state.store
    if not body.username.strip() or len(body.password) < 4:
        raise HTTPException(400, "Username required and password must be >= 4 chars")
    if await store.get_user_by_username(body.username):
        raise HTTPException(409, "Username already taken")
    user_id = new_user_id()
    await store.create_user(user_id, body.username, hash_password(body.password))
    token = new_token()
    await store.create_token(token, user_id, token_expiry())
    return {"token": token, "user": {"user_id": user_id, "username": body.username}}


@app.post("/api/auth/login")
async def login(body: Credentials):
    store: Store = app.state.store
    user = await store.get_user_by_username(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    token = new_token()
    await store.create_token(token, user["user_id"], token_expiry())
    return {
        "token": token,
        "user": {"user_id": user["user_id"], "username": user["username"]},
    }


@app.get("/api/auth/me")
async def me(user: dict = Depends(current_user)):
    return {"user": user}


@app.post("/api/auth/logout")
async def logout(
    authorization: str | None = Header(None), user: dict = Depends(current_user)
):
    store: Store = app.state.store
    token = _bearer(authorization)
    if token:
        await store.delete_token(token)
    return {"ok": True}


# --- browser sessions --------------------------------------------------------
@app.post("/api/sessions")
async def create_session(body: CreateSession, user: dict = Depends(current_user)):
    store: Store = app.state.store
    registry: SessionRegistry = app.state.registry
    provider = body.provider or settings().browser_provider
    session_id = "s_" + uuid.uuid4().hex[:12]
    name = body.name or f"session-{session_id[2:8]}"
    await store.create_session(session_id, user["user_id"], name, provider)
    try:
        await registry.create(session_id, provider)  # opens the browser now
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            500,
            f"Browser failed to start ({exc}). If local: run "
            f"`uv run playwright install chromium`.",
        )
    return {"session_id": session_id, "name": name, "provider": provider}


@app.get("/api/sessions")
async def list_sessions(user: dict = Depends(current_user)):
    store: Store = app.state.store
    registry: SessionRegistry = app.state.registry
    rows = await store.list_sessions(user["user_id"])
    for r in rows:
        r["live"] = registry.is_live(r["session_id"])
    return {"sessions": rows}


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str, user: dict = Depends(current_user)):
    store: Store = app.state.store
    registry: SessionRegistry = app.state.registry
    if await store.session_owner(session_id) != user["user_id"]:
        raise HTTPException(404, "Session not found")
    row = await store.get_session(session_id)
    live = registry.is_live(session_id)
    detail = dict(row)
    detail["live"] = live
    if live:
        s = registry.get(session_id)
        detail["live_view_mode"] = s.live_view_mode
        detail["live_view_url"] = s.live_view_url
        detail["lease"] = registry.lease_state(session_id)
    return detail


# --- chats -------------------------------------------------------------------
@app.post("/api/chats")
async def create_chat(body: CreateChat, user: dict = Depends(current_user)):
    store: Store = app.state.store
    if await store.session_owner(body.session_id) != user["user_id"]:
        raise HTTPException(404, "Session not found")
    chat_id = "c_" + uuid.uuid4().hex[:12]
    title = body.title or "New chat"
    await store.create_chat(chat_id, body.session_id, user["user_id"], title)
    return {"chat_id": chat_id, "session_id": body.session_id, "title": title}


@app.get("/api/chats")
async def list_chats(session_id: str | None = None, user: dict = Depends(current_user)):
    store: Store = app.state.store
    return {"chats": await store.list_chats(user["user_id"], session_id)}


@app.get("/api/chats/{chat_id}")
async def chat_detail(chat_id: str, user: dict = Depends(current_user)):
    store: Store = app.state.store
    chat = await store.get_chat(chat_id)
    if not chat or chat["user_id"] != user["user_id"]:
        raise HTTPException(404, "Chat not found")
    return chat


@app.get("/api/chats/{chat_id}/messages")
async def chat_messages(chat_id: str, user: dict = Depends(current_user)):
    store: Store = app.state.store
    if await store.chat_owner(chat_id) != user["user_id"]:
        raise HTTPException(404, "Chat not found")
    return {"messages": await store.export_messages(chat_id)}


@app.get("/api/chats/{chat_id}/steps")
async def chat_steps(chat_id: str, user: dict = Depends(current_user)):
    store: Store = app.state.store
    if await store.chat_owner(chat_id) != user["user_id"]:
        raise HTTPException(404, "Chat not found")
    return {"steps": await store.list_steps(chat_id)}


@app.get("/api/artifacts/{chat_id}/{filename}")
async def get_artifact(
    chat_id: str,
    filename: str,
    token: str | None = None,
    authorization: str | None = Header(None),
):
    # token may arrive as a header (fetch) or ?token= (so <img> tags work);
    # only the owner of the chat may read its screenshots.
    store: Store = app.state.store
    tok = token or _bearer(authorization)
    user_id = await store.user_for_token(tok) if tok else None
    if not user_id:
        raise HTTPException(401, "Unauthorized")
    if await store.chat_owner(chat_id) != user_id:
        raise HTTPException(404, "Not found")
    if "/" in filename or ".." in filename or "/" in chat_id or ".." in chat_id:
        raise HTTPException(400, "Bad path")
    base = Path(settings().artifacts_dir).resolve()
    path = (base / chat_id / filename).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/health")
async def health():
    return {"ok": True}


# --- chat plane (streamed) ---------------------------------------------------
@app.websocket("/ws/chat/{chat_id}")
async def chat_ws(ws: WebSocket, chat_id: str):
    await ws.accept()
    store: Store = ws.app.state.store
    registry: SessionRegistry = ws.app.state.registry
    runner: Runner = ws.app.state.runner

    user_id = await user_for_ws(store, ws.query_params.get("token"))
    if not user_id:
        await ws.close(code=4401)
        return
    chat = await store.get_chat(chat_id)
    if not chat or chat["user_id"] != user_id:
        await ws.close(code=4404)
        return
    session_id = chat["session_id"]

    async def emit(ev: StreamEvent) -> None:
        try:
            await ws.send_text(json.dumps({"type": ev.type, "data": dict(ev.data)}))
        except Exception:  # noqa: BLE001 — client went away mid-stream
            pass

    tasks: set = ws.app.state.tasks
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("kind")
            if kind == "user_message":
                # keep a reference so the fire-and-forget turn isn't GC'd mid-run
                task = asyncio.create_task(
                    runner.run_turn(session_id, chat_id, msg["text"], emit)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif kind == "approval":
                await runner.submit_approval(chat_id, msg["decisions"])
    except WebSocketDisconnect:
        # unblock any run_turn waiting on an approval from this (now gone) client,
        # so it unwinds and releases the lease instead of deadlocking.
        runner.cancel_pending(chat_id)
        registry.detach(session_id, chat_id)


# --- live view + takeover plane ---------------------------------------------
@app.websocket("/ws/view/{session_id}")
async def view_ws(ws: WebSocket, session_id: str):
    await ws.accept()
    store: Store = ws.app.state.store
    registry: SessionRegistry = ws.app.state.registry

    user_id = await user_for_ws(store, ws.query_params.get("token"))
    if not user_id:
        await ws.close(code=4401)
        return
    if await store.session_owner(session_id) != user_id:
        await ws.close(code=4404)
        return
    if not await registry.ensure(session_id):
        await ws.close(code=4404)
        return

    session = registry.get(session_id)

    await ws.send_text(
        json.dumps(
            {
                "type": "live_view",
                "mode": session.live_view_mode,
                "url": session.live_view_url,
            }
        )
    )

    async def send_frame(b64: str) -> None:
        try:
            await ws.send_text(json.dumps({"type": "frame", "data": b64}))
        except Exception:  # noqa: BLE001
            pass

    if session.live_view_mode == "screencast":
        session.subscribe(send_frame)

    lease_token: str | None = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg["kind"]
            if kind == "take_over":
                lease = await registry.acquire(session_id, "human", user_id)
                lease_token = lease.token if lease else None
                await ws.send_text(
                    json.dumps({"type": "lease", "granted": lease is not None,
                                "driver": "human" if lease else
                                registry.lease_state(session_id)["driver"]})
                )
            elif kind == "release" and lease_token:
                await registry.release(session_id, lease_token)
                lease_token = None
                await ws.send_text(json.dumps({"type": "lease", "granted": False,
                                               "driver": "none"}))
            elif kind == "mouse" and lease_token:   # local only; iframe handles its own
                await session.inject_mouse(
                    msg["x"], msg["y"], msg.get("event", "mousePressed")
                )
            elif kind == "key" and lease_token:
                await session.inject_key(msg["key"], msg.get("text"))
    except WebSocketDisconnect:
        if session.live_view_mode == "screencast":
            session.unsubscribe(send_frame)
        if lease_token:
            await registry.release(session_id, lease_token)


# --- optionally serve the built frontend ------------------------------------
_DIST = ROOT / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
