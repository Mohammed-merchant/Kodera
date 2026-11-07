"""Liveness and readiness endpoints."""

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.core.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch dependencies."""
    return {"status": "ok", "version": __version__}


async def _check_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(redis: Redis) -> None:
    await redis.ping()


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    """Readiness: Postgres and Redis are reachable. Returns 503 if either fails."""
    checks: dict[str, str] = {}
    for name, coro in (
        ("postgres", _check_postgres(request.app.state.db_engine)),
        ("redis", _check_redis(request.app.state.redis)),
    ):
        try:
            await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT_SECONDS)
            checks[name] = "ok"
        except Exception as exc:
            log.warning("readiness_check_failed", dependency=name, error=repr(exc))
            checks[name] = "error"

    is_ready = all(v == "ok" for v in checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if is_ready else "unavailable", "checks": checks}
