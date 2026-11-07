from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Call, CallEvent
from app.db.repositories.calls import add_event, get_call, upsert_call

SID = "CA" + "1" * 32


async def test_upsert_creates_then_updates_same_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        first = await upsert_call(
            s, SID, direction="inbound", from_number="+15551230000", to_number="+15550000000"
        )
        second = await upsert_call(s, SID, status="in-progress")
        await s.commit()
        assert first.id == second.id
        count = await s.scalar(select(func.count()).select_from(Call))
        assert count == 1
        assert second.status == "in-progress"
        assert second.from_number == "+15551230000"


async def test_finished_call_status_is_not_overwritten(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        await upsert_call(s, SID, status="completed", duration_seconds=12)
        call = await upsert_call(s, SID, status="in-progress")
        assert call.status == "completed"
        assert call.ended_at is not None
        assert call.duration_seconds == 12


async def test_add_event_is_linked_to_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        call = await upsert_call(s, SID)
        await add_event(s, call, "call.started", {"x": 1})
        await s.commit()
        events = (await s.execute(select(CallEvent))).scalars().all()
        assert [(e.type, e.data, e.call_id) for e in events] == [
            ("call.started", {"x": 1}, call.id)
        ]
        assert (await get_call(s, SID)) is not None
        assert (await get_call(s, "CA" + "9" * 32)) is None
