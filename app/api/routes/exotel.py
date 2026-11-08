"""Exotel webhook routes.

Exotel call flow (built in the Exotel dashboard, App Bazaar):
    ExoPhone -> Passthru applet -> Voicebot applet (wss://DOMAIN/ws/exotel)
The Passthru applet calls /webhooks/exotel/passthru when a call arrives. A call-status
callback (if configured) goes to /webhooks/exotel/status.
Both URLs must carry ?token=<EXOTEL_WEBHOOK_SECRET>.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.business.call_manager import record_call_started, record_call_status
from app.core.config import Settings, get_settings
from app.core.exotel_security import ExotelData, require_provider
from app.core.privacy import mask_number
from app.providers import exotel as exotel_provider

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/webhooks/exotel",
    tags=["webhooks"],
    dependencies=[Depends(require_provider("exotel"))],
)


def _check_account(data: dict[str, str], settings: Settings) -> None:
    """If the request names an account, it must be ours (AgentStream Passthru sends AccountSid)."""
    named = data.get("AccountSid", "")
    expected = settings.exotel_account_sid
    if named and expected and named != expected:
        log.warning("exotel_wrong_account")
        raise HTTPException(status_code=403, detail="Forbidden")


@router.api_route("/passthru", methods=["GET", "POST"], include_in_schema=False)
async def exotel_passthru(
    request: Request,
    data: ExotelData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """A call reached the Passthru applet: save it, then let the flow continue (HTTP 200)."""
    _check_account(data, settings)
    try:
        call = exotel_provider.parse_call(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid call data") from exc

    log.info(
        "exotel_call_received",
        call_sid=call.call_sid,
        caller=mask_number(call.from_number),
        called=mask_number(call.to_number),
        direction=call.direction,
    )
    try:
        await record_call_started(
            request.app.state.session_factory,
            request.app.state.redis,
            call_sid=call.call_sid,
            from_number=call.from_number,
            to_number=call.to_number,
            direction=call.direction,
            provider="exotel",
        )
    except Exception:
        # Saving must never break the call flow: Exotel halts the call on a non-200 answer.
        log.exception("call_save_failed", call_sid=call.call_sid)
    # Empty 200 body: the flow continues to the next applet (the Voicebot).
    return Response(status_code=200)


@router.api_route("/status", methods=["GET", "POST"], include_in_schema=False)
async def exotel_status(
    request: Request,
    data: ExotelData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Call status change. Errors return 500 so Exotel can retry (it retries non-2xx)."""
    _check_account(data, settings)
    call_sid = data.get("CallSid", "")
    if not exotel_provider.is_valid_call_sid(call_sid):
        raise HTTPException(status_code=400, detail="Invalid CallSid")
    try:
        status = exotel_provider.parse_status(data)
        duration = exotel_provider.parse_duration(data)
    except ValueError as exc:
        # The status fields are not documented by Exotel: log field NAMES (never values)
        # so the first live callback shows what Exotel really sends.
        log.warning("exotel_status_unrecognised", fields=sorted(data))
        raise HTTPException(status_code=400, detail="Invalid status data") from exc

    saved = await record_call_status(
        request.app.state.session_factory,
        request.app.state.redis,
        call_sid=call_sid,
        status=status,
        duration_seconds=duration,
        provider="exotel",
    )
    log.info(
        "exotel_status_received",
        call_sid=call_sid,
        status=status,
        duplicate=not saved,
        fields=sorted(data),
    )
    return Response(status_code=200)
