from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.core.config import Settings
from app.pipeline.tools import build_tools

SETTINGS = Settings(_env_file=None, clinic_sessions="10:00-11:00,17:00-18:00")
SCHED = appts.schedule_from_settings(SETTINGS)
Factory = async_sessionmaker[AsyncSession]


def next_open_day() -> date:
    """A weekday (Mon-Sat) at least 2 days ahead, so every slot is in the future."""
    day = SCHED.now().date() + timedelta(days=2)
    while day.weekday() not in SCHED.open_days:
        day += timedelta(days=1)
    return day


def at(day: date, hhmm: str) -> datetime:
    clock = appts.parse_clock(hhmm)
    assert clock
    return datetime.combine(day, clock, tzinfo=SCHED.tz)


def sunday_after(day: date) -> date:
    return day + timedelta(days=6 - day.weekday())


def test_slots_follow_the_sessions() -> None:
    day = next_open_day()
    slots = appts.slots_on(SCHED, day)
    assert [f"{s:%H:%M}" for s in slots][:2] == ["10:00", "10:15"]
    assert len(slots) == 8  # 4 in the morning session + 4 in the evening one
    assert appts.slots_on(SCHED, sunday_after(day)) == []  # closed on Sunday


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("17:30", (17, 30)),
        ("5:30 pm", (17, 30)),
        ("5 PM", (17, 0)),
        ("12 am", (0, 0)),
        ("9.15", (9, 15)),
    ],
)
def test_parse_clock(text: str, expected: tuple[int, int]) -> None:
    parsed = appts.parse_clock(text)
    assert parsed and (parsed.hour, parsed.minute) == expected


def test_parse_clock_and_phone_reject_nonsense() -> None:
    assert appts.parse_clock("soon") is None and appts.parse_clock("25:00") is None
    assert appts.normalize_phone("+91 98765-43210") == "9876543210"
    assert appts.normalize_phone("123") is None


def test_parse_day_words() -> None:
    today = SCHED.now().date()
    assert appts.parse_day("today", SCHED) == today
    assert appts.parse_day("Tomorrow", SCHED) == today + timedelta(days=1)
    assert appts.parse_day("2030-01-05", SCHED) == date(2030, 1, 5)
    assert appts.parse_day("next week", SCHED) is None
    assert appts.parse_day("2030-01-05T10:00:00", SCHED) == date(2030, 1, 5)
    thursday = appts.parse_day("Thursday", SCHED)
    assert thursday and thursday.weekday() == 3 and thursday >= today
    clock = appts.parse_clock("10:00:00")
    assert clock and (clock.hour, clock.minute) == (10, 0)


async def book(factory: Factory, slot: datetime, name: str = "Asha Rao", phone: str = "9876543210"):
    return await appts.book(
        factory, SCHED, name=name, phone=phone, slot=slot, reason="fever", call_sid="CA1"
    )


async def test_book_then_slot_is_taken(session_factory: Factory) -> None:
    day = next_open_day()
    first = await book(session_factory, at(day, "17:00"))
    assert first.ok and "Booked" in first.message
    free = await appts.free_slots(session_factory, SCHED, day)
    assert at(day, "17:00") not in free and at(day, "17:15") in free
    second = await book(session_factory, at(day, "17:00"), "Ravi Kumar", "9123456780")
    assert not second.ok  # never double-booked


async def test_cannot_book_off_grid_or_past_slots(session_factory: Factory) -> None:
    day = next_open_day()
    assert not (await book(session_factory, at(day, "17:07"))).ok
    assert not (await book(session_factory, at(day - timedelta(days=30), "17:00"))).ok


async def test_cancel_frees_the_slot(session_factory: Factory) -> None:
    day = next_open_day()
    await book(session_factory, at(day, "10:00"))
    assert len(await appts.upcoming(session_factory, SCHED, "9876543210")) == 1
    assert (await appts.cancel(session_factory, SCHED, "9876543210", None)).ok
    assert at(day, "10:00") in await appts.free_slots(session_factory, SCHED, day)
    assert not (await appts.cancel(session_factory, SCHED, "9876543210", None)).ok


async def test_stored_utc_matches_clinic_time(session_factory: Factory) -> None:
    day = next_open_day()
    await book(session_factory, at(day, "17:00"))
    (appt,) = await appts.upcoming(session_factory, SCHED, "9876543210")
    assert appts.as_aware(appt.slot_start) == at(day, "17:00").astimezone(UTC)
    assert "5 PM" in appts.speak_slot(appts.as_aware(appt.slot_start), SCHED)


# ----- the tools the LLM calls -----


class FakeParams:
    def __init__(self) -> None:
        self.result: dict | None = None

    async def result_callback(self, result: dict) -> None:
        self.result = result


async def call(tools: list, name: str, **kwargs: object) -> dict:
    fn = next(t for t in tools if t.__name__ == name)
    params = FakeParams()
    await fn(params, **kwargs)
    assert params.result is not None
    return params.result


async def test_tools_book_flow(session_factory: Factory) -> None:
    tools = build_tools(SETTINGS, session_factory, caller_number="+919876543210", call_sid="CA9")
    assert {t.__name__ for t in tools} == {
        "check_available_slots",
        "book_appointment",
        "cancel_appointment",
        "my_appointments",
    }
    day = next_open_day().isoformat()

    slots = await call(tools, "check_available_slots", date=day)
    assert slots["ok"] and slots["free_times"][0] == "10:00"

    args = {"patient_name": "Asha Rao", "date": day, "time": "10:00", "reason": "fever"}
    assert (await call(tools, "book_appointment", **args))["ok"]
    assert not (await call(tools, "book_appointment", **{**args, "patient_name": "Ravi"}))["ok"]
    assert len((await call(tools, "my_appointments"))["appointments"]) == 1
    assert (await call(tools, "cancel_appointment", date=day))["ok"]


async def test_tools_handle_bad_input(session_factory: Factory) -> None:
    tools = build_tools(SETTINGS, session_factory, caller_number="", call_sid="")
    assert not (await call(tools, "check_available_slots", date="whenever"))["ok"]
    day = next_open_day()
    no_phone = await call(
        tools,
        "book_appointment",
        patient_name="Asha Rao",
        date=day.isoformat(),
        time="10:00",
        reason="",
    )
    assert not no_phone["ok"] and "phone" in no_phone["message"]
    closed = await call(tools, "check_available_slots", date=sunday_after(day).isoformat())
    assert not closed["ok"] and "closed" in closed["message"]


async def test_a_broken_tool_tells_the_agent_not_to_promise(session_factory: Factory) -> None:
    class BrokenFactory:
        def __call__(self) -> None:
            raise RuntimeError("database is down")

    tools = build_tools(SETTINGS, BrokenFactory(), caller_number="9876543210", call_sid="CA1")  # type: ignore[arg-type]
    result = await call(tools, "check_available_slots", date=next_open_day().isoformat())
    assert not result["ok"] and "do not promise" in result["message"]


async def test_extra_arguments_from_the_model_are_ignored(session_factory: Factory) -> None:
    tools = build_tools(SETTINGS, session_factory, caller_number="9876543210", call_sid="CA1")
    day = next_open_day().isoformat()
    result = await call(tools, "check_available_slots", date=day, time="10:00", extra="x")
    assert result["ok"]


async def test_the_earliest_slot_of_the_day_is_taken_after_booking(
    session_factory: Factory,
) -> None:
    """10:00 clinic time is 04:30 UTC: the lookup must compare in UTC, or it forgets the booking."""
    day = next_open_day()
    assert (await book(session_factory, at(day, "10:00"))).ok
    assert at(day, "10:00") not in await appts.free_slots(session_factory, SCHED, day)
    assert not (await book(session_factory, at(day, "10:00"), "Ravi Kumar", "9123456780")).ok
