"""Staff dashboard: bookings, calls and database performance.

GET  /dashboard               the page (HTML only; it contains no data)
POST /dashboard/api/login     {"password": "..."} -> sets the session cookie
POST /dashboard/api/logout
GET  /dashboard/api/overview  all numbers as JSON (needs the cookie)
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.business.dashboard_stats import build_overview
from app.core.config import Settings, get_settings
from app.core.dashboard_auth import (
    COOKIE_NAME,
    client_ip,
    limiter,
    make_token,
    password_is_correct,
    require_dashboard_login,
)
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)

PAGE = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    # Everything is in the one file; nothing is loaded from other sites.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
    ),
}


class LoginBody(BaseModel):
    password: str


@router.get("")
async def page(settings: Annotated[Settings, Depends(get_settings)]) -> HTMLResponse:
    if not settings.dashboard_password.get_secret_value():
        raise HTTPException(status_code=404)
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers=PAGE_HEADERS)


@router.post("/api/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, bool]:
    if not settings.dashboard_password.get_secret_value():
        raise HTTPException(status_code=404)
    ip = client_ip(request)
    if limiter.blocked(ip):
        log.warning("dashboard_login_blocked", ip=ip)
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in 15 minutes.")
    if not password_is_correct(settings, body.password):
        limiter.record_failure(ip)
        log.warning("dashboard_login_failed", ip=ip)
        raise HTTPException(status_code=401, detail="Wrong password")
    limiter.clear(ip)
    response.set_cookie(
        COOKIE_NAME,
        make_token(settings),
        max_age=settings.dashboard_session_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=settings.app_env == "production",
        path="/dashboard",
    )
    response.headers["Cache-Control"] = "no-store"
    log.info("dashboard_login", ip=ip)
    return {"ok": True}


@router.post("/api/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/dashboard")
    return {"ok": True}


@router.get("/api/overview", dependencies=[Depends(require_dashboard_login)])
async def overview(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    state = request.app.state
    return await build_overview(state.session_factory, state.redis, settings)
