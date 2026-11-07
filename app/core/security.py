"""Security helpers.

Twilio webhook signature check lives here. Future: Telnyx webhook signatures
(Ed25519 with TELNYX_PUBLIC_KEY) and the GATEWAY_API_KEY check for admin routes.
"""

from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request
from starlette.datastructures import QueryParams
from twilio.request_validator import RequestValidator

from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)

TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"


def twilio_request_url(request: Request, public_base_url: str) -> str:
    """Rebuild the exact URL Twilio called.

    Behind Caddy the app sees http and an internal host, so the signed URL is built
    from PUBLIC_BASE_URL (https, real domain) plus the request path and query.
    """
    url = public_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


async def verify_twilio_signature(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> QueryParams:
    """FastAPI dependency: reject requests Twilio did not sign.

    Returns the parsed form fields so the route does not need to read the body again.
    """
    token = settings.twilio_auth_token.get_secret_value()
    if not token:
        log.error("twilio_auth_token_not_configured")
        raise HTTPException(status_code=503, detail="Twilio is not configured")

    signature = request.headers.get(TWILIO_SIGNATURE_HEADER)
    if not signature:
        raise HTTPException(status_code=403, detail="Missing signature")

    # Form body is application/x-www-form-urlencoded; parse it without python-multipart.
    body = (await request.body()).decode("utf-8")
    params = QueryParams(body)
    url = twilio_request_url(request, settings.public_base_url)

    if not RequestValidator(token).validate(url, params, signature):
        log.warning("twilio_signature_invalid", url=url)
        raise HTTPException(status_code=403, detail="Invalid signature")
    return params


TwilioParams = Annotated[QueryParams, Depends(verify_twilio_signature)]
