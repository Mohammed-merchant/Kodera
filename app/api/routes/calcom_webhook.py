"""Receives Cal.diy webhooks so bookings made or changed in Cal.diy show up in our database.

In Cal.diy: Settings > Developer > Webhooks > New, subscriber URL
    https://YOUR_DOMAIN/webhooks/calcom
triggers BOOKING_CREATED, BOOKING_RESCHEDULED, BOOKING_CANCELLED, and the same secret as
CAL_WEBHOOK_SECRET. Without that secret set, this endpoint refuses everything.
"""

from typing import Annotated

import orjson
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from app.business.calcom_sync import handle_event, signature_is_valid
from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)

MAX_BODY_BYTES = 262_144


@router.post("/calcom")
async def calcom_webhook(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, str]:
    secret = settings.cal_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail="Cal.diy webhook is not configured")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Body too large")
    if not signature_is_valid(secret, body, request.headers.get("x-cal-signature-256")):
        log.warning("calcom_webhook_bad_signature")
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        event = orjson.loads(body)
        if not isinstance(event, dict):
            raise ValueError
    except (orjson.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    result = await handle_event(request.app.state.session_factory, event)
    log.info("calcom_webhook", trigger=event.get("triggerEvent"), result=result)
    return {"result": result}
