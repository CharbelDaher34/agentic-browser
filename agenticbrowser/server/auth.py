"""Authentication.

Stdlib pbkdf2 password hashing (no native bcrypt build needed), bearer login
tokens persisted in `auth_sessions`, and FastAPI/WS helpers that resolve the
current user from a token. REST passes the token as `Authorization: Bearer ...`;
WebSockets pass it as a `?token=` query param (headers are awkward over WS).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Header, HTTPException, Request

from .config import settings
from .store import Store

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _algo, iters, salt_hex, hash_hex = encoded.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


def new_user_id() -> str:
    return uuid.uuid4().hex


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=settings().token_ttl_hours)


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return authorization  # tolerate a raw token


async def current_user(request: Request, authorization: str | None = Header(None)) -> dict:
    """FastAPI dependency: returns {user_id, username} or raises 401."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    store: Store = request.app.state.store
    user_id = await store.user_for_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = await store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return user


async def user_for_ws(store: Store, token: str | None) -> str | None:
    """Resolve a user_id from a WS `?token=` query param, or None."""
    if not token:
        return None
    return await store.user_for_token(token)
