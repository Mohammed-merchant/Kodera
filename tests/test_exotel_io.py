"""The Exotel <-> LiveKit audio bridge and the per-reply guard."""

import asyncio
import base64
from typing import Any

from livekit import rtc

from app.pipeline import exotel_io
from app.pipeline.exotel_io import ExotelAudioInput, ExotelAudioOutput, _take_chunk
from app.pipeline.guards import SAFE_NOT_BOOKED, ReplyGuard


def frame(seconds: float, rate: int = 8000) -> rtc.AudioFrame:
    n = int(rate * seconds)
    return rtc.AudioFrame(
        data=b"\x01\x00" * n, sample_rate=rate, num_channels=1, samples_per_channel=n
    )


def test_every_chunk_is_a_valid_exotel_size() -> None:
    for size in (1, 319, 3199, 3200, 6399, 6400, 99_999, 100_000, 250_000):
        pending = bytearray(size)
        while (chunk := _take_chunk(pending, ended=True)) is not None:
            assert len(chunk) % 320 == 0 and 3200 <= len(chunk) <= 100_000
        assert not pending


def test_small_audio_waits_until_the_reply_ends() -> None:
    pending = bytearray(1000)
    assert _take_chunk(pending, ended=False) is None
    assert len(_take_chunk(pending, ended=True) or b"") == 3200  # padded with silence


async def test_input_turns_exotel_payloads_into_frames() -> None:
    audio = ExotelAudioInput()
    audio.push(b"\x00" * 321)  # an odd byte is kept for the next payload
    audio.push(b"\x00")
    audio.close()
    frames = [f async for f in audio]
    assert [f.samples_per_channel for f in frames] == [160, 1]
    assert frames[0].sample_rate == 8000


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


async def test_output_sends_paced_chunks_and_reports_playback(monkeypatch: Any) -> None:
    monkeypatch.setattr(exotel_io, "LEAD_SECONDS", 10.0)  # no real-time waiting in the test
    sock = FakeSocket()
    out = ExotelAudioOutput(sock.send, "SID")
    await out.capture_frame(frame(1.0))
    out.flush()
    event = await asyncio.wait_for(out.wait_for_playout(), 3)
    assert not event.interrupted and abs(event.playback_position - 1.0) < 0.01
    payloads = [base64.b64decode(m["media"]["payload"]) for m in sock.sent]
    assert sum(len(p) for p in payloads) == 16_000
    assert all(m["event"] == "media" and m["stream_sid"] == "SID" for m in sock.sent)
    await out.aclose()


async def test_interruption_clears_exotel_and_reports_it(monkeypatch: Any) -> None:
    sock = FakeSocket()
    out = ExotelAudioOutput(sock.send, "SID")
    await out.capture_frame(frame(5.0))
    out.flush()
    await asyncio.sleep(0.05)
    out.clear_buffer()
    event = await asyncio.wait_for(out.wait_for_playout(), 1)
    assert event.interrupted and event.playback_position < 1.0
    await asyncio.sleep(0.05)
    assert sock.sent[-1] == {"event": "clear", "stream_sid": "SID"}
    sent_seconds = (
        sum(
            len(base64.b64decode(m["media"]["payload"])) for m in sock.sent if m["event"] == "media"
        )
        / 16_000
    )
    assert sent_seconds < 1.0  # only a little was ahead of the caller's ear
    await out.aclose()


class Recorder:
    booked = cancelled = False
    checked = True

    def __init__(self) -> None:
        self.events: list[str] = []

    async def add_event(self, kind: str, **_: Any) -> None:
        self.events.append(kind)


async def test_reply_guard_replaces_a_false_booking_claim() -> None:
    rec = Recorder()
    guard = ReplyGuard(rec)
    spoken = []
    for piece in ["I have booked your ", "appointment. ", "May I have ", "your name?"]:
        spoken += await guard.feed(piece)
    spoken += await guard.end()
    assert "".join(spoken) == f"{SAFE_NOT_BOOKED} May I have your name?"
    assert rec.events == ["guard"]


async def test_reply_guard_releases_a_half_sentence_at_the_end() -> None:
    guard = ReplyGuard(Recorder())
    assert await guard.feed("Let me check") == []
    assert await guard.end() == ["Let me check"]
