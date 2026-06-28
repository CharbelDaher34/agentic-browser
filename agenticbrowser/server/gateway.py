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
import base64
import json
import struct
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

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
from ..artifacts import LocalArtifacts
from ..models import ProviderName, StreamEvent
from ..recorder import Recorder
from ..registry import SessionRegistry
from ..runner import Runner
from .settings import ROOT, settings
from .store_sql import Store


async def _bootstrap_user(store: Store) -> None:
    """Create the single bootstrapped account on startup (idempotent). Combined
    with allow_registration=False, this is how a public deploy is locked to one
    login: the password lives only in env (BOOTSTRAP_PASSWORD), never in code."""
    s = settings()
    if not (s.bootstrap_username and s.bootstrap_password):
        return
    if await store.get_user_by_username(s.bootstrap_username):
        return
    try:
        await store.create_user(
            new_user_id(), s.bootstrap_username, hash_password(s.bootstrap_password)
        )
    except IntegrityError:
        # Another replica/worker created the same bootstrap user between the
        # check above and this insert (concurrent startup). Idempotent: the row
        # exists, which is the desired end state. (create_user is unchanged, so
        # the registration endpoint still surfaces duplicates as a 409.)
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = await Store.connect(settings().database_url)
    await _bootstrap_user(store)
    cfg = settings().to_core_config()
    registry = SessionRegistry(store, cfg)
    recorder = Recorder(store, LocalArtifacts(settings().artifacts_dir))
    runner = Runner(registry, store, recorder, cfg)
    app.state.store = store
    app.state.registry = registry
    app.state.recorder = recorder
    app.state.runner = runner
    app.state.tasks = set()   # keep refs to fire-and-forget run_turn tasks
    app.state.runs = {}       # run_id -> state for REST fire-and-forget runs
    app.state.run_tasks = {}  # run_id -> driver task (for POST /runs/{id}/stop)

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
@app.get("/api/auth/config")
async def auth_config():
    # public: lets the UI hide the sign-up form when registration is disabled
    return {"allow_registration": settings().allow_registration}


@app.post("/api/auth/register")
async def register(body: Credentials):
    if not settings().allow_registration:
        raise HTTPException(403, "Registration is disabled")
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


# --- app config (public-ish: tells the UI what to ask for) -------------------
def _server_browserbase() -> bool:
    """True if the server has Browserbase creds configured (from its env)."""
    s = settings()
    return bool(s.browserbase_api_key and s.browserbase_project_id)


@app.get("/api/config")
async def app_config():
    s = settings()
    return {
        "browser_provider": s.browser_provider,
        # only true if the server is misconfigured (browserbase provider, no creds)
        "browserbase_required": s.browser_provider == "browserbase" and not _server_browserbase(),
    }


# --- browser sessions --------------------------------------------------------
@app.post("/api/sessions")
async def create_session(body: CreateSession, user: dict = Depends(current_user)):
    store: Store = app.state.store
    registry: SessionRegistry = app.state.registry
    s = settings()
    provider = body.provider or s.browser_provider
    if provider == "browserbase" and not _server_browserbase():
        raise HTTPException(
            400, "Browserbase isn't configured — set BROWSERBASE_API_KEY and "
            "BROWSERBASE_PROJECT_ID on the server.",
        )

    session_id = "s_" + uuid.uuid4().hex[:12]
    name = body.name or f"session-{session_id[2:8]}"
    await store.create_session(session_id, user["user_id"], name, provider)
    try:
        await registry.create(session_id, provider)  # opens the browser now
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "Browserbase credentials" in msg:
            raise HTTPException(400, msg)
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
        detail["tabs"] = s.list_tabs()
        detail["lease_states"] = registry.lease_states(session_id)
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
async def chat_steps(chat_id: str, since: int = 0, user: dict = Depends(current_user)):
    store: Store = app.state.store
    if await store.chat_owner(chat_id) != user["user_id"]:
        raise HTTPException(404, "Chat not found")
    return {"steps": await store.list_steps(chat_id, since)}


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


# --- fire-and-forget runs (self-host service; poll GET /api/runs/{id}) --------
class RunIn(BaseModel):
    text: str


class RunApproval(BaseModel):
    decisions: dict


@app.post("/api/chats/{chat_id}/runs")
async def start_run(chat_id: str, body: RunIn, user: dict = Depends(current_user)):
    """Start a turn fire-and-forget; returns {run_id}. Poll progress/result via
    GET /api/runs/{run_id}; approve a paused destructive action via
    POST /api/runs/{run_id}/approvals; cancel via POST /api/runs/{run_id}/stop."""
    store: Store = app.state.store
    runner: Runner = app.state.runner
    if await store.chat_owner(chat_id) != user["user_id"]:
        raise HTTPException(404, "Chat not found")
    session_id = await store.session_of(chat_id)
    if not session_id:
        raise HTTPException(404, "Chat has no session")
    run_id = "run_" + uuid.uuid4().hex[:12]
    state = {
        "run_id": run_id, "chat_id": chat_id, "status": "running",
        "output": None, "usage": None, "error": None, "events": 0,
    }
    runs = app.state.runs
    if len(runs) > 2000:  # bound memory: evict oldest TERMINAL runs only (never a live/paused one)
        terminal = [rid for rid, st in runs.items()
                    if st["status"] in ("succeeded", "failed", "cancelled")]
        for old in terminal[: len(runs) - 2000]:
            runs.pop(old, None)
    runs[run_id] = state

    async def emit(ev: StreamEvent) -> None:
        state["events"] += 1
        if ev.type == "final":
            state["output"] = ev.data.get("text")
        elif ev.type == "usage":
            state["usage"] = dict(ev.data)
        elif ev.type == "error":
            state["error"] = ev.data.get("msg")

    async def driver() -> None:
        try:
            await runner.run_turn(session_id, chat_id, body.text, emit, user["user_id"])
            state["status"] = "failed" if state["error"] else "succeeded"
        except asyncio.CancelledError:
            state["status"] = "cancelled"  # POST /runs/{id}/stop unwinds run_turn cleanly
            raise
        except Exception as exc:  # noqa: BLE001
            state["status"], state["error"] = "failed", str(exc)
        finally:
            app.state.run_tasks.pop(run_id, None)

    task = asyncio.create_task(driver())
    app.state.run_tasks[run_id] = task
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
    return {"run_id": run_id, "status": "running"}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(current_user)):
    state = app.state.runs.get(run_id)
    store: Store = app.state.store
    if not state or await store.chat_owner(state["chat_id"]) != user["user_id"]:
        raise HTTPException(404, "Run not found")
    return state


@app.post("/api/runs/{run_id}/approvals")
async def approve_run(run_id: str, body: RunApproval, user: dict = Depends(current_user)):
    """Resolve a run paused on a destructive action (seen via GET /api/runs/{id}
    as an `approval_request`). decisions: {tool_call_id: true | "deny reason"}."""
    state = app.state.runs.get(run_id)
    store: Store = app.state.store
    runner: Runner = app.state.runner
    if not state or await store.chat_owner(state["chat_id"]) != user["user_id"]:
        raise HTTPException(404, "Run not found")
    await runner.submit_approval(state["chat_id"], body.decisions)
    return {"ok": True}


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str, user: dict = Depends(current_user)):
    """Cancel a fire-and-forget run; run_turn unwinds cleanly (releases its lease,
    persists partial context). Status becomes 'cancelled'."""
    state = app.state.runs.get(run_id)
    store: Store = app.state.store
    if not state or await store.chat_owner(state["chat_id"]) != user["user_id"]:
        raise HTTPException(404, "Run not found")
    task = app.state.run_tasks.get(run_id)
    if task and not task.done():
        task.cancel()
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

    # Ordered, single-writer streaming: agent tools emit `action`/`observation`
    # from PydanticAI's internal graph task, while the runner emits `token`/
    # `tool_call` from the event-consumer task. Those are two concurrent tasks,
    # so calling ws.send_text from both races (interleaved/corrupt frames). We
    # funnel every event through one queue (put_nowait preserves production
    # order) and a single sender task is the only thing that touches the socket.
    send_q: asyncio.Queue = asyncio.Queue()

    async def emit(ev: StreamEvent) -> None:
        send_q.put_nowait(ev)

    async def sender() -> None:
        while True:
            ev = await send_q.get()
            try:
                await ws.send_text(json.dumps(ev.wire()))
            except Exception:  # noqa: BLE001 — client went away mid-stream
                break
            finally:
                send_q.task_done()

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            try:
                raw = await ws.receive_text()
            except RuntimeError:
                # Client vanished (page refresh / abrupt close): Starlette raises
                # RuntimeError ("not connected"), not WebSocketDisconnect, when the
                # socket is already torn down. Normalize so the same cleanup runs.
                raise WebSocketDisconnect() from None
            msg = json.loads(raw)
            kind = msg.get("kind")
            if kind == "user_message":
                # start_turn INTERRUPTS any in-flight turn first (mid-run steering),
                # persisting partial context, then launches the new turn. The runner
                # owns the task reference, so it isn't GC'd mid-run.
                await runner.start_turn(session_id, chat_id, msg["text"], emit, user_id)
            elif kind == "interrupt":
                await runner.stop(chat_id)
            elif kind == "approval":
                await runner.submit_approval(chat_id, msg["decisions"])
    except WebSocketDisconnect:
        # stop the running turn so it unwinds, releases every lease, and persists
        # partial context instead of deadlocking on a now-gone client.
        await runner.stop(chat_id)
        runner.cancel_pending(chat_id)
        registry.detach(session_id, chat_id)
    finally:
        sender_task.cancel()


# --- live view + takeover plane ---------------------------------------------
_FRAME_TICK = object()  # sentinel: "a fresh frame is waiting in the coalescing slot"


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

    # Single-writer streaming (mirrors chat_ws): the CDP screencast callback fires
    # from a separate task and the receive loop also sends (lease/tabs). Writing the
    # socket from both races and trips the websockets keepalive AssertionError, so
    # ALL writes funnel through one queue drained by one sender task.
    send_q: asyncio.Queue = asyncio.Queue()
    # frame coalescing: keep only the newest JPEG so a slow client gets the freshest
    # frame, never a growing backlog.
    latest = {"b64": None, "tab_id": None, "pending": False}

    def enqueue(obj) -> None:
        send_q.put_nowait(obj)

    async def sender() -> None:
        while True:
            item = await send_q.get()
            try:
                if item is _FRAME_TICK:
                    latest["pending"] = False
                    b64, tab = latest["b64"], latest["tab_id"]
                    if b64 is None:
                        continue
                    latest["b64"] = None
                    w, h = session.screen_size_of(tab)
                    # binary frame: [ver=1][w u16][h u16][tab_len u8][tab][jpeg]
                    # ~35% smaller than base64-in-JSON, no quality loss.
                    jpeg = base64.b64decode(b64)
                    tb = (tab or "t0").encode()
                    header = struct.pack(">BHHB", 1, w & 0xFFFF, h & 0xFFFF, len(tb))
                    await ws.send_bytes(header + tb + jpeg)
                else:
                    await ws.send_text(json.dumps(item))  # control msgs stay JSON
            except Exception:  # noqa: BLE001 — client went away
                break

    sender_task = asyncio.create_task(sender())

    enqueue({"type": "live_view", "mode": session.live_view_mode,
             "url": session.live_view_url})
    enqueue({"type": "tabs", "tabs": session.list_tabs(),
             "leases": registry.lease_states(session_id)})

    # ---- per-tab frame subscription (one watched tab at a time) ----
    sub = {"fn": None, "tab": None}

    def watch(tab_id: str) -> None:
        if session.live_view_mode != "screencast" or not session.has_tab(tab_id):
            return
        if sub["fn"] is not None:
            session.unsubscribe(sub["fn"], sub["tab"])

        async def on_frame(b64: str, _tab=tab_id) -> None:
            latest["b64"], latest["tab_id"] = b64, _tab
            if not latest["pending"]:
                latest["pending"] = True
                enqueue(_FRAME_TICK)

        sub["fn"], sub["tab"] = on_frame, tab_id
        session.subscribe(on_frame, tab_id)

    async def push_initial(tab_id: str) -> None:
        # Hand the viewer the current page right away — the CDP screencast only
        # emits on repaint, so a static page would otherwise show no frame.
        if session.live_view_mode != "screencast" or not session.has_tab(tab_id):
            return
        b64 = await session.frame_jpeg_b64(tab_id)
        if b64:
            # route through the same coalescing slot so it ships as a binary frame
            latest["b64"], latest["tab_id"] = b64, tab_id
            if not latest["pending"]:
                latest["pending"] = True
                enqueue(_FRAME_TICK)

    watch("t0")
    await push_initial("t0")

    leases: dict[str, str] = {}   # tab_id -> human lease token

    def _send_lease(tab_id: str, granted: bool) -> None:
        driver = "human" if granted else registry.lease_state(session_id, tab_id)["driver"]
        enqueue({"type": "lease", "granted": granted, "driver": driver, "tab_id": tab_id})

    try:
        while True:
            try:
                raw = await ws.receive_text()
            except RuntimeError:
                # Client vanished (page refresh / abrupt close): Starlette raises
                # RuntimeError ("not connected"), not WebSocketDisconnect, when the
                # socket is already torn down. Normalize to a clean disconnect — the
                # `finally` below still unsubscribes and releases any leases.
                raise WebSocketDisconnect() from None
            msg = json.loads(raw)
            kind = msg["kind"]
            tab_id = msg.get("tab_id") or sub["tab"] or "t0"
            if kind == "watch":
                wt = msg.get("tab_id") or "t0"
                watch(wt)
                await push_initial(wt)
            elif kind == "tabs":
                enqueue({"type": "tabs", "tabs": session.list_tabs(),
                         "leases": registry.lease_states(session_id)})
            elif kind == "take_over":
                lease = await registry.acquire(session_id, "human", user_id, tab_id=tab_id)
                if lease:
                    leases[tab_id] = lease.token
                _send_lease(tab_id, lease is not None)
            elif kind == "release" and leases.get(tab_id):
                await registry.release(session_id, leases.pop(tab_id), tab_id=tab_id)
                _send_lease(tab_id, False)
            elif kind == "mouse" and leases.get(tab_id):   # local only; iframe self-handles
                await session.inject_mouse(
                    msg["x"], msg["y"], msg.get("event", "mousePressed"), tab_id=tab_id
                )
            elif kind == "key" and leases.get(tab_id):
                await session.inject_key(msg["key"], msg.get("text"), tab_id=tab_id)
            elif kind == "scroll" and leases.get(tab_id):
                await session.inject_scroll(
                    msg["x"], msg["y"], msg.get("dx", 0), msg.get("dy", 0), tab_id=tab_id
                )
    except WebSocketDisconnect:
        pass
    finally:
        if sub["fn"] is not None:
            session.unsubscribe(sub["fn"], sub["tab"])
        for tab_id, token in list(leases.items()):
            await registry.release(session_id, token, tab_id=tab_id)
        sender_task.cancel()


# --- serve the built frontend (single-origin: no Vite/proxy in prod) ---------
# app.frontend() registers the static build as LOW-priority routes, so all the
# /api and /ws routes above are matched first; unknown browser paths fall back to
# index.html (SPA routing). Build it with `cd frontend && npm run build`.
_DIST = ROOT / "frontend" / "dist"
if settings().serve_ui and _DIST.is_dir():
    # fallback="auto": serve index.html for browser-navigation requests (SPA
    # routing) but let unmatched /api/* fetches return a real 404, not HTML.
    # SERVE_UI=false → pure API deployment (no bundled UI).
    app.frontend("/", directory=str(_DIST), fallback="auto")
