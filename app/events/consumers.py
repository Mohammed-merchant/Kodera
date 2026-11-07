"""Event consumer: read the call-event stream and hand each event to a handler."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.events.publisher import STREAM

log = structlog.get_logger(__name__)

GROUP = "workers"
Handler = Callable[[dict[str, Any]], Awaitable[None]]


def parse_entry(fields: dict[Any, Any]) -> dict[str, Any]:
    """Turn raw stream fields (bytes or str) into a plain event dict."""

    def text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    raw = {text(k): text(v) for k, v in fields.items()}
    return {
        "type": raw.get("type", ""),
        "call_sid": raw.get("call_sid", ""),
        "payload": orjson.loads(raw.get("payload", "{}")),
    }


async def ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):  # group already exists: fine
            raise


async def consume_forever(redis: Redis, handler: Handler, consumer: str = "worker-1") -> None:
    """Read new events, call the handler, then acknowledge. Runs until cancelled."""
    await ensure_group(redis)
    while True:
        try:
            batches = await redis.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=10, block=5000)
            for _stream, entries in batches or []:
                for entry_id, fields in entries:
                    await handler(parse_entry(fields))
                    await redis.xack(STREAM, GROUP, entry_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event_consumer_error")
            await asyncio.sleep(2)  # do not spin if Redis is down
