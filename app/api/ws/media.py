"""Media stream WebSocket: Twilio sends the caller's audio here.

For now it only echoes each audio frame back (echo test). Later the Pipecat voice
pipeline will run here, in this process.

Security: Twilio passes back the token we put in the TwiML (see build_voice_twiml);
audio is only accepted after that token is verified. Every connection also has a
hard time limit so a stuck call cannot run up the Twilio bill.
"""

import asyncio
import contextlib
from typing import Annotated, Any

import orjson
import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.business.call_manager import record_stream_event
from app.core.config import Settings, get_settings
from app.core.security import verify_stream_token

log = structlog.get_logger(__name__)

router = APIRouter()

START_TIMEOUT_SECONDS = 10  # the start event must arrive quickly
MAX_MESSAGE_CHARS = 16_384  # one 20 ms audio frame is far smaller
POLICY_VIOLATION = 1008


async def _save(ws: WebSocket, call_sid: str, event_type: str, data: dict[str, Any]) -> None:
    try:
        await record_stream_event(ws.app.state.session_factory, call_sid, event_type, data)
    except Exception:
        log.exception("stream_event_save_failed", call_sid=call_sid, event=event_type)


async def _close_quietly(ws: WebSocket, code: int = 1000) -> None:
    with contextlib.suppress(RuntimeError):  # already closed
        await ws.close(code=code)


@router.websocket("/ws/media")
async def media_stream(ws: WebSocket, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if settings.call_provider != "twilio":
        await ws.close(code=POLICY_VIOLATION)
        return
    await ws.accept()
    call_sid = ""
    stream_sid = ""
    frames = 0
    reason = "closed"
    try:
        async with asyncio.timeout(settings.max_call_seconds):
            while True:
                if stream_sid:
                    raw = await ws.receive_text()
                else:
                    raw = await asyncio.wait_for(ws.receive_text(), START_TIMEOUT_SECONDS)
                if len(raw) > MAX_MESSAGE_CHARS:
                    reason = "message too large"
                    await _close_quietly(ws, POLICY_VIOLATION)
                    break
                try:
                    message = orjson.loads(raw)
                    event = message.get("event")
                except (orjson.JSONDecodeError, AttributeError):
                    reason = "bad message"
                    await _close_quietly(ws, POLICY_VIOLATION)
                    break

                if event == "start":
                    start = message.get("start") or {}
                    sid = str(start.get("callSid", ""))
                    token = str((start.get("customParameters") or {}).get("token", ""))
                    secret = settings.twilio_auth_token.get_secret_value()
                    if not verify_stream_token(sid, token, secret):
                        log.warning("media_stream_bad_token")
                        reason = "bad token"
                        await _close_quietly(ws, POLICY_VIOLATION)
                        break
                    call_sid = sid
                    stream_sid = str(start.get("streamSid", ""))
                    log.info("media_stream_started", call_sid=call_sid)
                    await _save(ws, call_sid, "stream.started", {})
                elif event == "connected":
                    continue
                elif not stream_sid:
                    # Audio or anything else before a valid start: not allowed.
                    reason = "no start event"
                    await _close_quietly(ws, POLICY_VIOLATION)
                    break
                elif event == "media":
                    media = message.get("media") or {}
                    if media.get("track", "inbound") == "inbound":
                        frames += 1
                        echo = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": media.get("payload", "")},
                        }
                        await ws.send_text(orjson.dumps(echo).decode())
                elif event == "stop":
                    reason = "stop"
                    break
    except (WebSocketDisconnect, RuntimeError):
        reason = "disconnected"
    except TimeoutError:
        reason = "time limit" if stream_sid else "no start event"
        await _close_quietly(ws)
    finally:
        if call_sid:
            log.info("media_stream_ended", call_sid=call_sid, frames=frames, reason=reason)
            await _save(ws, call_sid, "stream.stopped", {"frames": frames, "reason": reason})
