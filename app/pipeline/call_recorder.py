"""Records one live call in Redis; app/business/call_flush.py saves it to Postgres at the end.

While the call runs, everything (transcript, bookings, timings) goes into three Redis keys.
Redis is fast and never blocks the caller. When the call ends the data is written to
Postgres in one go. If that fails the data stays in Redis and a background job retries.

    callrec:<sid>:meta    hash   who called, when, last activity
    callrec:<sid>:turns   list   {role, text, at, response_ms, stt_ms, llm_ms, tts_ms}
    callrec:<sid>:events  list   {kind, at, ...}  (booking, cancel, guard)
"""

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis

log = structlog.get_logger(__name__)

OP_TIMEOUT_SECONDS = 2.0  # one Redis operation
DRAIN_TIMEOUT_SECONDS = 3.0  # waiting for the queue when the call ends
KEY_TTL_SECONDS = 24 * 3600  # a call that never got saved is still kept a day for the retry job


def keys(call_sid: str) -> tuple[str, str, str]:
    return (f"callrec:{call_sid}:meta", f"callrec:{call_sid}:turns", f"callrec:{call_sid}:events")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CallRecorder:
    """Writes to Redis from a background queue, in order, so a slow or dead Redis can never
    hold up the call: the methods below return at once."""

    def __init__(self, redis: Redis, call_sid: str, *, provider: str, caller: str) -> None:
        self.redis = redis
        self.call_sid = call_sid
        self.provider = provider
        self.caller = caller
        self.meta_key, self.turns_key, self.events_key = keys(call_sid)
        # Facts the reply guard needs: did a booking / cancel really succeed in this call?
        self.booked = False
        self.cancelled = False
        self._queue: asyncio.Queue[tuple[str, Callable[[], Awaitable[Any]]]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    # ---- queue ----
    def _enqueue(self, what: str, make: Callable[[], Awaitable[Any]]) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        self._queue.put_nowait((what, make))

    async def _run(self) -> None:
        while True:
            what, make = await self._queue.get()
            try:
                await asyncio.wait_for(make(), OP_TIMEOUT_SECONDS)
            except Exception:
                log.exception("call_recorder_failed", call_sid=self.call_sid, step=what)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait (briefly) until everything queued has reached Redis."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._queue.join(), DRAIN_TIMEOUT_SECONDS)

    # ---- what gets recorded ----
    async def start(self) -> None:
        async def go() -> None:
            await self.redis.hset(  # type: ignore[misc]
                self.meta_key,
                mapping={
                    "provider": self.provider,
                    "caller": self.caller,
                    "started_at": _now(),
                    "last_activity": str(time.time()),
                    "state": "live",
                },
            )
            await self.redis.expire(self.meta_key, KEY_TTL_SECONDS)

        self._enqueue("start", go)

    def _push(self, key: str, item: dict[str, Any]) -> Callable[[], Awaitable[Any]]:
        async def go() -> None:
            pipe = self.redis.pipeline()
            pipe.rpush(key, json.dumps(item))
            pipe.expire(key, KEY_TTL_SECONDS)
            pipe.hset(self.meta_key, "last_activity", str(time.time()))
            await pipe.execute()

        return go

    async def add_turn(self, role: str, text: str, **timings: int | None) -> None:
        if not text.strip():
            return
        item: dict[str, Any] = {"role": role, "text": text.strip(), "at": _now()}
        item.update({k: v for k, v in timings.items() if v is not None})
        self._enqueue("turn", self._push(self.turns_key, item))

    async def add_event(self, kind: str, **data: Any) -> None:
        self._enqueue("event", self._push(self.events_key, {"kind": kind, "at": _now(), **data}))

    async def finish(self, reason: str) -> None:
        async def go() -> None:
            await self.redis.hset(  # type: ignore[misc]
                self.meta_key, mapping={"state": "ended", "ended_at": _now(), "reason": reason}
            )

        self._enqueue("finish", go)
        await self.drain()
        if self._worker:
            self._worker.cancel()
