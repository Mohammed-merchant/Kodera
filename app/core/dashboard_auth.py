"""Login for the staff dashboard: one shared password, then a signed cookie.

The cookie holds only an expiry time and an HMAC of it, so nothing is stored on the server.
Changing DASHBOARD_PASSWORD signs everyone out. Failed logins are rate limited per IP.
"""

import hashlib
import hmac
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.core.config import Settings, get_settings

COOKIE_NAME = "kodera_dashboard"
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60


def _key(settings: Settings) -> bytes:
    password = settings.dashboard_password.get_secret_value()
    return hashlib.sha256(b"kodera-dashboard:" + password.encode()).digest()


def make_token(settings: Settings, now: float | None = None) -> str:
    expires = int((now or time.time()) + settings.dashboard_session_hours * 3600)
    sig = hmac.new(_key(settings), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def token_is_valid(settings: Settings, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    expected = hmac.new(_key(settings), expires.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return expires.isdigit() and int(expires) > (now or time.time())


def password_is_correct(settings: Settings, attempt: str) -> bool:
    real = settings.dashboard_password.get_secret_value()
    return bool(real) and hmac.compare_digest(attempt.encode(), real.encode())


class LoginLimiter:
    """Blocks an IP after too many wrong passwords. In memory: fine for one server."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}

    def _recent(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(ip, []) if now - t < FAILURE_WINDOW_SECONDS]
        self._failures[ip] = recent
        return recent

    def blocked(self, ip: str, now: float | None = None) -> bool:
        return len(self._recent(ip, now or time.time())) >= MAX_FAILURES

    def record_failure(self, ip: str, now: float | None = None) -> None:
        self._recent(ip, now or time.time()).append(now or time.time())

    def clear(self, ip: str) -> None:
        self._failures.pop(ip, None)


limiter = LoginLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_dashboard_login(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    """Router dependency for the data endpoints."""
    if not settings.dashboard_password.get_secret_value():
        raise HTTPException(status_code=404)
    if not token_is_valid(settings, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not signed in")
