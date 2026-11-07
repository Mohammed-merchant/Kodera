"""Event publisher: after-call events go to a Redis Stream."""

from typing import Any

import orjson
from redis.asyncio import Redis

STREAM = "events:calls"
STREAM_MAX_LEN = 10_000  # keep the stream small on a 2 GB server


async def publish_event(
    redis: Redis, event_type: str, call_sid: str, payload: dict[str, Any] | None = None
) -> str:
    """Add one event (for example call.started or call.completed) to the stream."""
    fields: dict[Any, Any] = {
        "type": event_type,
        "call_sid": call_sid,
        "payload": orjson.dumps(payload or {}).decode(),
    }
    entry_id: bytes | str = await redis.xadd(
        STREAM, fields, maxlen=STREAM_MAX_LEN, approximate=True
    )
    return entry_id.decode() if isinstance(entry_id, bytes) else entry_id
