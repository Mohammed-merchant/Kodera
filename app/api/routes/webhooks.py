"""Carrier webhook routes.

Twilio voice webhook: Twilio calls it when someone dials our number. Every request
is signature-checked before any of our code runs. Telnyx routes will be added later.
"""

import re
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from twilio.twiml.voice_response import VoiceResponse

from app.core.config import Settings, get_settings
from app.core.security import TwilioParams

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])

# The greeting also discloses that the caller is talking to an AI.
GREETING = "Hello, this is our AI assistant. Goodbye."

_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")


def _greeting_twiml() -> str:
    response = VoiceResponse()
    response.say(GREETING)
    return str(response)


# Built once: the greeting never changes per request.
GREETING_TWIML = _greeting_twiml()


def mask_number(number: str) -> str:
    """Keep only the last 4 digits so phone numbers never land in logs in full."""
    return f"***{number[-4:]}" if len(number) > 4 else "***"


@router.post("/voice", include_in_schema=False)
async def twilio_voice(
    params: TwilioParams,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Answer an incoming call with the greeting (TwiML)."""
    # Only accept calls that belong to our own Twilio account.
    expected_sid = settings.twilio_account_sid
    if expected_sid and params.get("AccountSid") != expected_sid:
        log.warning("twilio_wrong_account")
        raise HTTPException(status_code=403, detail="Wrong account")

    call_sid = params.get("CallSid", "")
    if not _CALL_SID.match(call_sid):
        raise HTTPException(status_code=400, detail="Invalid CallSid")

    log.info(
        "twilio_call_received",
        call_sid=call_sid,
        caller=mask_number(params.get("From", "")),
        called=mask_number(params.get("To", "")),
        direction=params.get("Direction", ""),
    )
    return Response(content=GREETING_TWIML, media_type="application/xml")
