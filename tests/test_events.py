from typing import Any, cast

from redis.asyncio import Redis

from app.events.consumers import parse_entry
from app.events.dedupe import first_time
from app.events.publisher import STREAM, publish_event
from tests.fakes import FakeRedis


def as_redis(fake: FakeRedis) -> Redis:
    return cast(Redis, fake)


async def test_first_time_is_true_only_once() -> None:
    redis = as_redis(FakeRedis())
    assert await first_time(redis, "twilio:CA1:completed") is True
    assert await first_time(redis, "twilio:CA1:completed") is False
    assert await first_time(redis, "twilio:CA1:ringing") is True


async def test_publish_and_parse_roundtrip() -> None:
    fake = FakeRedis()
    await publish_event(as_redis(fake), "call.completed", "CA1", {"duration": 7})
    _entry_id, fields = fake.streams[STREAM][0]
    event: dict[str, Any] = parse_entry(fields)
    assert event == {"type": "call.completed", "call_sid": "CA1", "payload": {"duration": 7}}


def test_parse_entry_accepts_bytes() -> None:
    event = parse_entry({b"type": b"call.started", b"call_sid": b"CA2", b"payload": b"{}"})
    assert event["type"] == "call.started"
    assert event["call_sid"] == "CA2"
