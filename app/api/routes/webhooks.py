"""Carrier webhook routes.

Twilio webhooks: Twilio calls them when someone dials our number and when the call
status changes. Every request is signature-checked before any of our code runs.
Telnyx routes will be added later.
"""

import re
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.datastructures import QueryParams
from twilio.twiml.voice_response import Connect, VoiceResponse

from app.business.call_manager import record_call_started, record_call_status
from app.core.config import Settings, get_settings
from app.core.exotel_security import require_provider
from app.core.privacy import mask_number
from app.core.security import TwilioParams, stream_token

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/webhooks/twilio",
    tags=["webhooks"],
    dependencies=[Depends(require_provider("twilio"))],
)

# The greeting also discloses that the caller is talking to an AI.
GREETING = "Hello, this is our AI assistant. Goodbye."
STREAM_GREETING = "Hello, this is our AI assistant."

_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_CALL_STATUSES = frozenset(
    {"queued", "ringing", "in-progress", "completed", "busy", "failed", "no-answer", "canceled"}
)


def _greeting_twiml() -> str:
    response = VoiceResponse()
    response.say(GREETING)
    return str(response)


# Built once: the greeting-only answer never changes per request.
GREETING_TWIML = _greeting_twiml()


def stream_url(settings: Settings) -> str:
    """wss:// address of our audio endpoint, built from PUBLIC_BASE_URL."""
    base = settings.public_base_url.rstrip("/")
    scheme_swapped = "wss://" + base.removeprefix("https://").removeprefix("http://")
    return f"{scheme_swapped}/ws/media"


def build_voice_twiml(call_sid: str, settings: Settings) -> str:
    """Greeting only, or greeting plus a two-way audio stream when streaming is enabled."""
    if not settings.twilio_stream_enabled:
        return GREETING_TWIML
    response = VoiceResponse()
    response.say(STREAM_GREETING)
    connect = Connect()
    stream = connect.stream(url=stream_url(settings))
    # Twilio sends this back in the stream's start event; /ws/media checks it.
    token = stream_token(call_sid, settings.twilio_auth_token.get_secret_value())
    stream.parameter(name="token", value=token)
    response.append(connect)
    return str(response)


def _checked_call_sid(params: QueryParams, settings: Settings) -> str:
    """Accept only calls of our own Twilio account with a well-formed CallSid."""
    expected_sid = settings.twilio_account_sid
    if expected_sid and params.get("AccountSid") != expected_sid:
        log.warning("twilio_wrong_account")
        raise HTTPException(status_code=403, detail="Wrong account")
    call_sid = params.get("CallSid", "")
    if not _CALL_SID.match(call_sid):
        raise HTTPException(status_code=400, detail="Invalid CallSid")
    return call_sid


@router.post("/voice", include_in_schema=False)
async def twilio_voice(
    request: Request,
    params: TwilioParams,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Answer an incoming call (TwiML) and save it."""
    call_sid = _checked_call_sid(params, settings)
    log.info(
        "twilio_call_received",
        call_sid=call_sid,
        caller=mask_number(params.get("From", "")),
        called=mask_number(params.get("To", "")),
        direction=params.get("Direction", ""),
    )
    try:
        await record_call_started(
            request.app.state.session_factory,
            request.app.state.redis,
            call_sid=call_sid,
            from_number=params.get("From", ""),
            to_number=params.get("To", ""),
            direction=params.get("Direction", ""),
        )
    except Exception:
        # Saving must never stop the caller from being answered.
        log.exception("call_save_failed", call_sid=call_sid)
    return Response(content=build_voice_twiml(call_sid, settings), media_type="application/xml")


@router.post("/status", include_in_schema=False)
async def twilio_status(
    request: Request,
    params: TwilioParams,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Save call status changes. Errors return 500 so Twilio can retry."""
    call_sid = _checked_call_sid(params, settings)
    status = params.get("CallStatus", "")
    if status not in _CALL_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid CallStatus")
    duration_raw = params.get("CallDuration", "")
    if duration_raw and not duration_raw.isdigit():
        raise HTTPException(status_code=400, detail="Invalid CallDuration")

    saved = await record_call_status(
        request.app.state.session_factory,
        request.app.state.redis,
        call_sid=call_sid,
        status=status,
        duration_seconds=int(duration_raw) if duration_raw else None,
    )
    log.info("twilio_status_received", call_sid=call_sid, status=status, duplicate=not saved)
    return Response(status_code=204)
