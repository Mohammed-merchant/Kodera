"""Call lifecycle: save calls and events, publish after-call events, skip duplicates."""

from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.calls import TERMINAL_STATUSES, add_event, upsert_call
from app.events.dedupe import first_time
from app.events.publisher import publish_event

log = structlog.get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


async def _record(
    session_factory: SessionFactory,
    redis: Redis,
    *,
    dedupe_key: str,
    call_sid: str,
    event_type: str,
    event_data: dict[str, Any],
    publish_as: str | None,
    **call_fields: Any,
) -> bool:
    """Save the call and one event once. Returns False if this exact event was seen before."""
    if not await first_time(redis, dedupe_key):
        log.info("duplicate_webhook_ignored", key=dedupe_key)
        return False
    try:
        async with session_factory() as session:
            call = await upsert_call(session, call_sid, **call_fields)
            await add_event(session, call, event_type, event_data)
            await session.commit()
        if publish_as:
            await publish_event(redis, publish_as, call_sid, event_data)
    except Exception:
        # Let a retry from the carrier through instead of swallowing it as a duplicate.
        await redis.delete(f"dedupe:{dedupe_key}")
        raise
    return True


async def record_call_started(
    session_factory: SessionFactory,
    redis: Redis,
    *,
    call_sid: str,
    from_number: str,
    to_number: str,
    direction: str,
    provider: str = "twilio",
) -> bool:
    return await _record(
        session_factory,
        redis,
        dedupe_key=f"{provider}:voice:{call_sid}",
        call_sid=call_sid,
        event_type="call.started",
        event_data={"direction": direction},
        publish_as="call.started",
        provider=provider,
        direction=direction,
        from_number=from_number,
        to_number=to_number,
        status="ringing",
    )


async def record_call_status(
    session_factory: SessionFactory,
    redis: Redis,
    *,
    call_sid: str,
    status: str,
    duration_seconds: int | None,
    provider: str = "twilio",
) -> bool:
    finished = status in TERMINAL_STATUSES
    data: dict[str, Any] = {"status": status}
    if duration_seconds is not None:
        data["duration_seconds"] = duration_seconds
    return await _record(
        session_factory,
        redis,
        dedupe_key=f"{provider}:status:{call_sid}:{status}",
        call_sid=call_sid,
        event_type=f"call.status.{status}",
        event_data=data,
        publish_as="call.completed" if finished else None,
        provider=provider,
        status=status,
        duration_seconds=duration_seconds,
    )


async def record_stream_event(
    session_factory: SessionFactory,
    call_sid: str,
    event_type: str,
    data: dict[str, Any],
    provider: str = "twilio",
) -> None:
    """Save a media-stream event (stream.started / stream.stopped) for a call."""
    async with session_factory() as session:
        call = await upsert_call(session, call_sid, provider=provider)
        await add_event(session, call, event_type, data)
        await session.commit()
