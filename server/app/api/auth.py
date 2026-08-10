"""Optional web authentication.

Designed for a trusted LAN: off by default, one operator account, signed
session cookie. When enabled, every API route except health and the login
endpoints requires a valid session (cookie) or bearer token.

No dependency on a session store or a JWT library — an HMAC over
``username|expiry`` is enough for one user and cannot be forged without the
server key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

SESSION_COOKIE = "turret_session"
DEFAULT_TTL_S = 30 * 24 * 3600


@dataclass(frozen=True)
class Session:
    username: str
    expires_at: float

    @property
    def valid(self) -> bool:
        return time.time() < self.expires_at


def _sign(key: str, payload: str) -> str:
    digest = hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def create_token(key: str, username: str, ttl_s: int = DEFAULT_TTL_S) -> str:
    expires_at = int(time.time() + ttl_s)
    payload = f"{username}|{expires_at}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(key, payload)}"


def verify_token(key: str, token: str) -> Session | None:
    """Validate a session token. Returns ``None`` for anything suspicious."""
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding).decode()
    except Exception:
        return None
    if not hmac.compare_digest(signature, _sign(key, payload)):
        return None
    username, _, expiry = payload.partition("|")
    try:
        session = Session(username=username, expires_at=float(expiry))
    except ValueError:
        return None
    return session if session.valid else None


def check_credentials(expected_user: str, expected_password: str, user: str, password: str) -> bool:
    """Constant-time credential comparison."""
    user_ok = hmac.compare_digest(expected_user.encode(), user.encode())
    password_ok = hmac.compare_digest(expected_password.encode(), password.encode())
    return user_ok and password_ok


def check_controller_token(expected: str, provided: str | None) -> bool:
    """Controller pre-shared key check. Empty expected token disables the check."""
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected.encode(), provided.encode())
