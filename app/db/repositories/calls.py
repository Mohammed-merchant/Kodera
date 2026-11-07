"""Call and call-event storage."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Call, CallEvent

TERMINAL_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})


async def get_call(session: AsyncSession, provider_call_id: str) -> Call | None:
    result = await session.execute(select(Call).where(Call.provider_call_id == provider_call_id))
    return result.scalar_one_or_none()


async def upsert_call(
    session: AsyncSession,
    provider_call_id: str,
    *,
    provider: str = "twilio",
    direction: str | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    status: str | None = None,
    duration_seconds: int | None = None,
) -> Call:
    """Create the call, or update it if the carrier id is already stored."""
    call = await get_call(session, provider_call_id)
    if call is None:
        call = Call(provider=provider, provider_call_id=provider_call_id, status="ringing")
        try:
            async with session.begin_nested():
                session.add(call)
        except IntegrityError:
            # Another request created it at the same moment: use that row.
            existing = await get_call(session, provider_call_id)
            if existing is None:
                raise
            call = existing

    if direction:
        call.direction = direction
    if from_number:
        call.from_number = from_number
    if to_number:
        call.to_number = to_number
    # A finished call never goes back to an earlier status (webhooks can arrive late).
    if status and call.status not in TERMINAL_STATUSES:
        call.status = status
        if status in TERMINAL_STATUSES:
            call.ended_at = datetime.now(UTC)
    if duration_seconds is not None:
        call.duration_seconds = duration_seconds
    await session.flush()
    return call


async def add_event(
    session: AsyncSession, call: Call, event_type: str, data: dict[str, Any] | None = None
) -> CallEvent:
    event = CallEvent(call_id=call.id, type=event_type, data=data or {})
    session.add(event)
    await session.flush()
    return event
