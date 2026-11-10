"""Exotel Voicebot WebSocket: Exotel sends the caller's audio here.

The voice agent (STT -> LLM -> TTS on the GPU server, see app/pipeline/) answers the call.
With VOICE_AGENT_ENABLED=false it falls back to the echo test (caller hears their own voice).

Security: the WebSocket handshake must carry the Basic auth Exotel documents (API key and
token embedded in the applet URL: wss://KEY:TOKEN@DOMAIN/ws/exotel). Without it the
connection is refused. Every connection also has a hard time limit.
"""

import asyncio
import base64
import contextlib
from typing import Annotated, Any

import orjson
import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.business.call_manager import record_stream_event
from app.core.config import Settings, get_settings
from app.pipeline.voice_pipeline import run_call
from app.providers import exotel as exotel_provider

log = structlog.get_logger(__name__)

router = APIRouter()

START_TIMEOUT_SECONDS = 10
MAX_MESSAGE_CHARS = 262_144  # a 100 KB audio chunk is ~133 KB of base64
POLICY_VIOLATION = 1008


async def _save(ws: WebSocket, call_sid: str, event_type: str, data: dict[str, Any]) -> None:
    try:
        await record_stream_event(
            ws.app.state.session_factory, call_sid, event_type, data, provider="exotel"
        )
    except Exception:
        log.exception("stream_event_save_failed", call_sid=call_sid, event_type=event_type)


async def _close_quietly(ws: WebSocket, code: int = 1000) -> None:
    with contextlib.suppress(RuntimeError):  # already closed
        await ws.close(code=code)


@router.websocket("/ws/exotel")
async def exotel_media_stream(
    ws: WebSocket, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    # Refuse before accepting: wrong carrier or missing/wrong credentials.
    if settings.call_provider != "exotel" or not exotel_provider.verify_basic_auth(
        ws.headers.get("authorization"),
        settings.exotel_api_key.get_secret_value(),
        settings.exotel_api_token.get_secret_value(),
    ):
        log.warning("exotel_ws_rejected")
        await ws.close(code=POLICY_VIOLATION)
        return

    await ws.accept()
    call_sid = ""
    stream_sid = ""
    inbound_frames = 0
    sent_chunks = 0
    buffer = bytearray()
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

                if event == "connected":
                    continue
                if event == "start":
                    start = message.get("start") or {}
                    sid = str(start.get("call_sid", ""))
                    account = str(start.get("account_sid", ""))
                    expected = settings.exotel_account_sid
                    if not exotel_provider.is_valid_call_sid(sid) or (
                        expected and account and account != expected
                    ):
                        reason = "bad start"
                        await _close_quietly(ws, POLICY_VIOLATION)
                        break
                    call_sid = sid
                    stream_sid = str(start.get("stream_sid") or message.get("stream_sid", ""))
                    log.info(
                        "exotel_stream_started",
                        call_sid=call_sid,
                        media_format=start.get("media_format"),
                    )
                    # Saved in the background so a slow database never delays the greeting.
                    saving = asyncio.create_task(_save(ws, call_sid, "stream.started", {}))
                    if settings.voice_agent_enabled:
                        # Pipecat takes over the socket and runs until the call ends.
                        reason = await run_call(
                            ws, stream_sid=stream_sid, call_sid=call_sid, settings=settings
                        )
                        await saving
                        await _close_quietly(ws)
                        break
                    await saving
                elif not stream_sid:
                    # Audio or anything else before a valid start: not allowed.
                    reason = "no start event"
                    await _close_quietly(ws, POLICY_VIOLATION)
                    break
                elif event == "media":
                    media = message.get("media") or {}
                    audio = exotel_provider.decode_audio(str(media.get("payload", "")))
                    if audio is None:
                        continue  # ignore a broken frame, keep the call alive
                    inbound_frames += 1
                    buffer.extend(audio)
                    while (chunk := exotel_provider.next_chunk(buffer)) is not None:
                        reply = {
                            "event": "media",
                            "stream_sid": stream_sid,
                            "media": {"payload": base64.b64encode(chunk).decode()},
                        }
                        await ws.send_text(orjson.dumps(reply).decode())
                        sent_chunks += 1
                elif event == "stop":
                    stop = message.get("stop") or {}
                    reason = str(stop.get("reason") or "stop")[:32]
                    break
                # dtmf, mark and other events are ignored for now.
    except (WebSocketDisconnect, RuntimeError):
        reason = "disconnected"
    except TimeoutError:
        reason = "time limit" if stream_sid else "no start event"
        await _close_quietly(ws)
    finally:
        if call_sid:
            log.info(
                "exotel_stream_ended",
                call_sid=call_sid,
                frames=inbound_frames,
                chunks_sent=sent_chunks,
                reason=reason,
            )
            await _save(
                ws,
                call_sid,
                "stream.stopped",
                {"frames": inbound_frames, "chunks_sent": sent_chunks, "reason": reason},
            )
