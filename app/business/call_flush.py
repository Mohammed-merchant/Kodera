"""Save a finished call from Redis to Postgres (transcript, timings, outcome).

Runs right after the call ends. If that fails the data stays in Redis, and
`flush_orphans` (a background job, every minute) saves it later, so a call is not lost
when the server restarts mid-call or Postgres is briefly down.
"""

import contextlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import TranscriptTurn
from app.db.repositories.calls import TERMINAL_STATUSES, add_event, upsert_call
from app.pipeline.call_recorder import keys

log = structlog.get_logger(__name__)

IDLE_SECONDS_BEFORE_RESCUE = 90  # a "live" call silent for this long is treated as dead
LOCK_SECONDS = 60


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def _read_meta(redis: Redis, key: str) -> dict[str, str]:
    """The app's Redis client returns bytes; make keys and values text."""
    raw = await redis.hgetall(key)  # type: ignore[misc]
    return {_text(k): _text(v) for k, v in raw.items()}


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def summarise(turns: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome and numbers for a call, from what was recorded."""
    kinds = [(e.get("kind"), bool(e.get("ok"))) for e in events]
    if ("booking", True) in kinds:
        outcome = "booked"
    elif ("cancel", True) in kinds:
        outcome = "cancelled"
    elif any(t["role"] == "caller" for t in turns):
        outcome = "info"
    else:
        outcome = "silent"
    responses = [t["response_ms"] for t in turns if t.get("response_ms") is not None]
    return {
        "outcome": outcome,
        "turns": len(turns),
        "avg_response_ms": round(sum(responses) / len(responses)) if responses else None,
        "guard_blocks": sum(1 for e in events if e.get("kind") == "guard"),
        "guard_reasons": [str(e.get("reason")) for e in events if e.get("kind") == "guard"],
    }


async def flush_call(
    redis: Redis, factory: async_sessionmaker[AsyncSession], call_sid: str
) -> bool:
    """Write one recorded call to Postgres and clear it from Redis. Safe to call twice."""
    meta_key, turns_key, events_key = keys(call_sid)
    try:
        if not await redis.set(f"{meta_key}:lock", "1", nx=True, ex=LOCK_SECONDS):
            return False  # another process is saving this call right now
    except Exception:
        log.exception("call_save_failed", call_sid=call_sid, step="lock")
        return False
    try:
        meta = await _read_meta(redis, meta_key)
        if not meta:
            return False
        turns = [json.loads(x) for x in await redis.lrange(turns_key, 0, -1)]  # type: ignore[misc]
        events = [json.loads(x) for x in await redis.lrange(events_key, 0, -1)]  # type: ignore[misc]
        summary = summarise(turns, events)
        now = datetime.now(UTC)
        started = _parse(meta.get("started_at")) or now
        ended = _parse(meta.get("ended_at")) or now

        async with factory() as session:
            await _write(session, call_sid, meta, turns, summary, started, ended)
            await session.commit()
        await redis.delete(meta_key, turns_key, events_key)
        log.info("call_saved", call_sid=call_sid, **summary)
        return True
    except Exception:
        log.exception("call_save_failed", call_sid=call_sid)  # kept in Redis; the job retries
        return False
    finally:
        with contextlib.suppress(Exception):
            await redis.delete(f"{meta_key}:lock")


async def _write(
    session: AsyncSession,
    call_sid: str,
    meta: dict[str, str],
    turns: list[dict[str, Any]],
    summary: dict[str, Any],
    started: datetime,
    ended: datetime,
) -> None:
    call = await upsert_call(
        session,
        call_sid,
        provider=meta.get("provider") or "exotel",
        from_number=meta.get("caller") or None,
    )
    already = await session.scalar(
        select(func.count()).select_from(TranscriptTurn).where(TranscriptTurn.call_id == call.id)
    )
    if already:  # saved before: do not duplicate the transcript
        return
    if call.status not in TERMINAL_STATUSES:
        call.status = "completed"
        call.ended_at = ended
    if call.duration_seconds is None:
        call.duration_seconds = max(0, round((ended - started).total_seconds()))
    call.outcome = summary["outcome"]
    call.turn_count = summary["turns"]
    call.avg_response_ms = summary["avg_response_ms"]
    for t in turns:
        session.add(
            TranscriptTurn(
                call_id=call.id,
                role=t["role"],
                text=t["text"],
                response_ms=t.get("response_ms"),
                stt_ms=t.get("stt_ms"),
                llm_ms=t.get("llm_ms"),
                tts_ms=t.get("tts_ms"),
                created_at=_parse(t.get("at")) or ended,
            )
        )
    await add_event(session, call, "call.summary", summary)


async def flush_orphans(redis: Redis, factory: async_sessionmaker[AsyncSession]) -> int:
    """Save calls that ended without being saved (crash, restart, database hiccup)."""
    saved = 0
    async for key in redis.scan_iter(match="callrec:*:meta", count=100):
        name = _text(key)
        meta = await _read_meta(redis, name)
        if not meta:
            continue
        idle = time.time() - float(meta.get("last_activity", 0) or 0)
        if meta.get("state") == "ended" or idle > IDLE_SECONDS_BEFORE_RESCUE:
            call_sid = name.removeprefix("callrec:").removesuffix(":meta")
            saved += await flush_call(redis, factory, call_sid)
    return saved
