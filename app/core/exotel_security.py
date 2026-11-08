"""Security for Exotel webhooks and the carrier switch.

Exotel documents no webhook signature, so a request is accepted only if:
1. Exotel is the active provider (CALL_PROVIDER=exotel),
2. the source IP is in the allowlist (only when EXOTEL_ALLOWED_IPS is set), and
3. the URL carries our secret: ?token=<EXOTEL_WEBHOOK_SECRET> (compared in constant time).
The WebSocket uses the Basic auth that Exotel documents (see providers/exotel.py).
"""

import hmac
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import orjson
import structlog
from fastapi import Depends, HTTPException, Request
from starlette.datastructures import QueryParams

from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)

MAX_BODY_BYTES = 65_536


def require_provider(name: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """Router dependency: routes of the inactive carrier answer 404, as if they did not exist."""

    async def check(settings: Annotated[Settings, Depends(get_settings)]) -> None:
        if settings.call_provider != name:
            raise HTTPException(status_code=404)

    return check


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


async def verify_exotel_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """FastAPI dependency: accept only requests that carry our secret; return their fields."""
    secret = settings.exotel_webhook_secret.get_secret_value()
    if not secret:
        log.error("exotel_webhook_secret_not_configured")
        raise HTTPException(status_code=503, detail="Exotel is not configured")

    allowed = settings.exotel_allowed_ip_list
    client_ip = request.client.host if request.client else ""
    if allowed and client_ip not in allowed:
        log.warning("exotel_webhook_ip_rejected", ip=client_ip)
        raise HTTPException(status_code=403, detail="Forbidden")

    query = dict(request.query_params)
    token = query.pop("token", "")
    if not hmac.compare_digest(token.encode(), secret.encode()):
        log.warning("exotel_webhook_bad_token")
        raise HTTPException(status_code=403, detail="Forbidden")

    fields: dict[str, str] = dict(query)
    if request.method == "POST":
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Body too large")
        if body:
            content_type = request.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    parsed = orjson.loads(body)
                except orjson.JSONDecodeError as exc:
                    raise HTTPException(status_code=400, detail="Invalid JSON") from exc
                if not isinstance(parsed, dict):
                    raise HTTPException(status_code=400, detail="Invalid JSON")
                fields.update({str(k): _as_text(v) for k, v in parsed.items()})
            else:
                fields.update(QueryParams(body.decode("utf-8", errors="replace")))
    return fields


ExotelData = Annotated[dict[str, str], Depends(verify_exotel_webhook)]
