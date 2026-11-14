"""Exotel Voicebot WebSocket as the audio input and output of a LiveKit AgentSession.

    Exotel  --media-->  ExotelAudioInput  -->  AgentSession (VAD, STT, LLM, TTS)
    Exotel  <--media--  ExotelAudioOutput <--

LiveKit Agents normally talks to a LiveKit room. Here the session reads and writes the Exotel
socket directly, so the phone side (Exotel applet, Passthru, Basic auth) does not change and no
LiveKit server sits in the middle of the call.

Audio on the Exotel socket is raw 16-bit little-endian mono PCM at 8 kHz, base64 encoded.
Chunks we send must be a multiple of 320 bytes and at least 3200 bytes (200 ms).
The session resamples the TTS audio to 8 kHz for us (it honours AudioOutput.sample_rate).
"""

import asyncio
import base64
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import orjson
import structlog
from fastapi import WebSocket, WebSocketDisconnect
from livekit import rtc
from livekit.agents.voice import io

from app.providers import exotel as exotel_provider

log = structlog.get_logger(__name__)

SAMPLE_RATE = 8_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
MAX_MESSAGE_CHARS = 262_144  # same limit as the WebSocket handler
# How far ahead of the caller's ear we send audio. Small, so an interruption stops the voice
# quickly (Exotel also gets a "clear"), but big enough that network jitter never starves it.
LEAD_SECONDS = 0.4
# Agent audio goes out in 400 ms chunks: a valid Exotel size (>= 3200 bytes, multiple of 320),
# and small enough that an interruption cuts the voice at once.
SEND_CHUNK_BYTES = 6_400
MAX_INPUT_FRAMES = 500  # ~10 s of caller audio; older audio is dropped if we fall behind

SendJson = Callable[[dict[str, Any]], Awaitable[None]]


class ExotelAudioInput(io.AudioInput):
    """Caller audio from Exotel, as LiveKit audio frames."""

    def __init__(self) -> None:
        super().__init__(label="ExotelInput")
        self._frames: asyncio.Queue[rtc.AudioFrame | None] = asyncio.Queue()
        self._odd = b""  # half a sample left over from the previous payload

    def push(self, pcm: bytes) -> None:
        data = self._odd + pcm
        usable = len(data) - len(data) % 2
        self._odd = data[usable:]
        if not usable:
            return
        if self._frames.qsize() >= MAX_INPUT_FRAMES:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._frames.get_nowait()
        self._frames.put_nowait(
            rtc.AudioFrame(
                data=data[:usable],
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=usable // 2,
            )
        )

    def close(self) -> None:
        self._frames.put_nowait(None)

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await self._frames.get()
        if frame is None:
            self._frames.put_nowait(None)  # stay closed for any later reader
            raise StopAsyncIteration
        return frame


def _take_chunk(pending: bytearray, ended: bool) -> bytes | None:
    """The next chunk to send, or None to wait for more audio. Always a valid Exotel size."""
    multiple = exotel_provider.CHUNK_MULTIPLE
    if len(pending) >= SEND_CHUNK_BYTES:
        size = SEND_CHUNK_BYTES
    elif len(pending) >= exotel_provider.MIN_CHUNK_BYTES and not ended:
        size = len(pending) // multiple * multiple
    elif ended and pending:
        # Last bit of the reply: pad with silence up to a size Exotel accepts.
        size = max(exotel_provider.MIN_CHUNK_BYTES, -(-len(pending) // multiple) * multiple)
    else:
        return None
    chunk = bytes(pending[:size]).ljust(size, b"\x00")
    del pending[:size]
    return chunk


@dataclass
class _Segment:
    """One reply (or one say()) of audio, from the first frame until flush()."""

    pending: bytearray = field(default_factory=bytearray)
    ended: bool = False
    sent_seconds: float = 0.0
    started_at: float | None = None  # when its first audio reaches the caller's ear
    done_timer: asyncio.TimerHandle | None = None


class ExotelAudioOutput(io.AudioOutput):
    """Agent audio to Exotel, sent in valid chunks at about real-time speed."""

    def __init__(self, send_json: SendJson, stream_sid: str) -> None:
        super().__init__(
            label="ExotelOutput",
            capabilities=io.AudioOutputCapabilities(pause=False),
            sample_rate=SAMPLE_RATE,
        )
        self._send_json = send_json
        self._stream_sid = stream_sid
        self._segments: deque[_Segment] = deque()  # being sent or still playing, oldest first
        self._capturing: _Segment | None = None
        self._wake = asyncio.Event()
        self._clock = 0.0  # when everything sent so far has finished playing
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    # ---- LiveKit AudioOutput interface ----
    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        if self._capturing is None:
            self._capturing = _Segment()
            self._segments.append(self._capturing)
        self._capturing.pending.extend(frame.data.tobytes())
        self._kick()

    def flush(self) -> None:
        super().flush()
        if self._capturing is not None:
            self._capturing.ended = True
            self._capturing = None
            self._kick()

    def clear_buffer(self) -> None:
        """The caller spoke over the agent: stop at once."""
        if not self._segments:
            return
        now = time.monotonic()
        for seg in self._segments:
            if seg.done_timer:
                seg.done_timer.cancel()
            played = 0.0
            if seg.started_at is not None:
                played = min(max(0.0, now - seg.started_at), seg.sent_seconds)
            self.on_playback_finished(playback_position=played, interrupted=True)
        self._segments.clear()
        self._capturing = None
        self._clock = now
        asyncio.get_running_loop().create_task(self._send_clear())

    # ---- sending ----
    def _kick(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._sender(), name="exotel_audio_sender")
        self._wake.set()

    async def _send_clear(self) -> None:
        with contextlib.suppress(Exception):
            await self._send_json({"event": "clear", "stream_sid": self._stream_sid})

    def _next_chunk(self) -> tuple[_Segment, bytes] | None:
        for seg in self._segments:
            if seg.done_timer is not None or (seg.ended and not seg.pending):
                continue  # fully sent, only playing out
            chunk = _take_chunk(seg.pending, seg.ended)
            return (seg, chunk) if chunk else None
        return None

    def _schedule_done(self, seg: _Segment) -> None:
        loop = asyncio.get_running_loop()

        def done() -> None:
            if seg in self._segments:
                self._segments.remove(seg)
                self.on_playback_finished(playback_position=seg.sent_seconds, interrupted=False)

        delay = max(0.0, self._clock - time.monotonic())
        seg.done_timer = loop.call_later(delay, done)

    async def _sender(self) -> None:
        try:
            while not self._closed:
                # Finished segments: report "played" when their audio has reached the ear.
                for seg in list(self._segments):
                    if seg.ended and not seg.pending and seg.done_timer is None:
                        if seg.sent_seconds == 0:
                            self._segments.remove(seg)
                            self.on_playback_finished(playback_position=0, interrupted=False)
                        else:
                            self._schedule_done(seg)
                item = self._next_chunk()
                if item is None:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                seg, chunk = item
                now = time.monotonic()
                ahead = self._clock - now
                if ahead > LEAD_SECONDS:
                    await asyncio.sleep(ahead - LEAD_SECONDS)
                    if seg not in self._segments:  # cleared while we waited
                        continue
                    now = time.monotonic()
                start = max(now, self._clock)
                seconds = len(chunk) / BYTES_PER_SECOND
                if seg.started_at is None:
                    seg.started_at = start
                    self.on_playback_started(created_at=time.time())
                self._clock = start + seconds
                seg.sent_seconds += seconds
                await self._send_json(
                    {
                        "event": "media",
                        "stream_sid": self._stream_sid,
                        "media": {"payload": base64.b64encode(chunk).decode()},
                    }
                )
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            self._closed = True  # the caller hung up; the session is being closed
        except asyncio.CancelledError:
            raise

    async def aclose(self) -> None:
        self._closed = True
        for seg in self._segments:
            if seg.done_timer:
                seg.done_timer.cancel()
        self._segments.clear()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


class ExotelSocket:
    """Owns the Exotel WebSocket for the duration of the agent call."""

    def __init__(self, ws: WebSocket, stream_sid: str) -> None:
        self.ws = ws
        self.stream_sid = stream_sid
        self.audio_input = ExotelAudioInput()
        self.audio_output = ExotelAudioOutput(self.send_json, stream_sid)
        self._send_lock = asyncio.Lock()
        self.end_reason = "call ended"

    async def send_json(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_text(orjson.dumps(message).decode())

    async def read_until_hangup(self) -> str:
        """Feed caller audio into the session until Exotel stops or the socket closes."""
        try:
            while True:
                raw = await self.ws.receive_text()
                if len(raw) > MAX_MESSAGE_CHARS:
                    self.end_reason = "message too large"
                    break
                try:
                    message = orjson.loads(raw)
                    event = message.get("event")
                except (orjson.JSONDecodeError, AttributeError):
                    continue  # one broken message must not drop the call
                if event == "media":
                    media = message.get("media") or {}
                    audio = exotel_provider.decode_audio(str(media.get("payload", "")))
                    if audio:
                        self.audio_input.push(audio)
                elif event == "stop":
                    self.end_reason = "call ended"
                    break
                # connected, dtmf, mark and other events are ignored.
        except (WebSocketDisconnect, RuntimeError):
            self.end_reason = "call ended"
        finally:
            self.audio_input.close()
        return self.end_reason
