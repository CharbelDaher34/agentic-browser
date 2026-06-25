"""Symmetric encryption for per-user API keys at rest (Fernet).

The key comes from `settings().app_secret` (env APP_SECRET). If unset, a key is
generated once and persisted to `ROOT/.app_secret` (gitignored) so encrypted keys
stay decryptable across restarts. An arbitrary secret string is accepted and
derived into a valid Fernet key.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet

from .config import ROOT, settings

_log = logging.getLogger(__name__)


def _derive(secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    secret = settings().app_secret
    if not secret:
        path = ROOT / ".app_secret"
        if path.exists():
            secret = path.read_text().strip()
        else:
            secret = Fernet.generate_key().decode()
            try:
                path.write_text(secret)
            except Exception:  # noqa: BLE001 — best effort; falls back to derived key
                pass
    try:
        return Fernet(secret.encode())          # already a valid Fernet key
    except Exception:  # noqa: BLE001
        return Fernet(_derive(secret))           # derive one from an arbitrary secret


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001 — wrong/rotated key → treat as unavailable
        # Surface this rather than silently dropping the user's key: it usually
        # means APP_SECRET changed (or differs from the generated .app_secret),
        # so every stored key now looks "missing" and BYOK falls back to env.
        _log.warning(
            "Could not decrypt a stored API key — APP_SECRET may have changed "
            "since it was saved; the user will need to re-enter the key."
        )
        return None
