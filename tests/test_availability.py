"""The agent must never promise a time it has not looked up, and must answer fast when it fails."""

from datetime import datetime, timedelta

import httpx
import pytest

from app.business import appointments as appts
from app.core.config import Settings
from app.pipeline import greeting
from app.pipeline.guards import SAFE_CHECK_FIRST, check_sentence
from app.pipeline.guards import SAFE_SAY_AGAIN as SAY_AGAIN
from app.pipeline.prompts import build_system_prompt
from app.pipeline.tools import build_tools
from tests.test_appointments import Factory, call, next_open_day

# 24 slots a day: far more than the 6 the old lookup returned.
BIG = Settings(_env_file=None, clinic_sessions="10:00-13:00,17:00-20:00")
SCHED = appts.schedule_from_settings(BIG)


def at(day, hhmm: str) -> datetime:
    clock = appts.parse_clock(hhmm)
    assert clock
    return datetime.combine(day, clock, tzinfo=SCHED.tz)


def tools_for(factory: Factory) -> list:
    return build_tools(BIG, factory, caller_number="9876543210", call_sid="CA1")


async def take(factory: Factory, slot: datetime, name: str = "Asha Rao") -> None:
    out = await appts.book(
        factory, SCHED, name=name, phone="9876543210", slot=slot, reason="", call_sid=""
    )
    assert out.ok, out.message


async def test_the_whole_day_is_returned_not_just_the_first_six(session_factory: Factory) -> None:
    day = next_open_day().isoformat()
    result = await call(tools_for(session_factory), "check_available_slots", date=day)
    assert result["ok"] and len(result["free_times"]) == 24
    assert "17:00" in result["free_times"] and "19:45" in result["free_times"]
    assert result["free_ranges_to_say"] == "10 AM to 12:45 PM and 5 PM to 7:45 PM"


async def test_a_taken_slot_splits_the_spoken_range(session_factory: Factory) -> None:
    day = next_open_day()
    await take(session_factory, at(day, "17:30"))
    result = await call(tools_for(session_factory), "check_available_slots", date=day.isoformat())
    assert "17:30" not in result["free_times"]
    assert result["free_ranges_to_say"] == (
        "10 AM to 12:45 PM and 5 PM to 5:15 PM and 5:45 PM to 7:45 PM"
    )


async def test_a_specific_time_can_be_checked(session_factory: Factory) -> None:
    day = next_open_day()
    await take(session_factory, at(day, "17:00"))
    tools = tools_for(session_factory)
    taken = await call(tools, "check_available_slots", date=day.isoformat(), time="5 PM")
    assert taken["time_is_free"] is False
    assert "NOT free" in taken["answer"] and "5:15 PM, 5:30 PM, 5:45 PM" in taken["answer"]
    free = await call(tools, "check_available_slots", date=day.isoformat(), time="18:00")
    assert free["time_is_free"] is True and "IS FREE" in free["answer"]


async def test_a_past_date_is_refused_with_todays_date(session_factory: Factory) -> None:
    tools = tools_for(session_factory)
    old = (SCHED.now().date() - timedelta(days=2)).isoformat()
    check = await call(tools, "check_available_slots", date=old)
    assert check["ok"] is False and "already passed" in check["message"]
    booking = await call(
        tools, "book_appointment", patient_name="Asha Rao", date=old, time="17:00", reason="x"
    )
    assert booking["ok"] is False and "already passed" in booking["message"]


def test_the_prompt_has_a_calendar_of_real_dates() -> None:
    prompt = build_system_prompt(BIG, SCHED.now())
    today = SCHED.now()
    assert f"{today:%Y-%m-%d} [today]" in prompt and "(clinic closed)" in prompt


def test_a_tool_call_written_as_text_is_never_spoken() -> None:
    for said in ('/book_appointment {"patient_name": "A"}', '{"date": "2026-11-16"}'):
        spoken, why = check_sentence(said, booked=False, cancelled=False, checked=True)
        assert spoken == SAY_AGAIN and why == "a tool call was written as text"


async def test_a_refused_booking_brings_the_nearest_times_with_it(session_factory: Factory) -> None:
    day = next_open_day().isoformat()
    tools = tools_for(session_factory)
    args = {"patient_name": "Asha Rao", "date": day, "time": "17:00", "reason": "fever"}
    assert (await call(tools, "book_appointment", **args))["ok"]
    again = await call(tools, "book_appointment", **{**args, "patient_name": "Ravi Kumar"})
    assert again["ok"] is False
    assert [a["time"] for a in again["alternatives"]] == ["16:45", "17:15", "17:30"] or len(
        again["alternatives"]
    ) == 3
    assert all(a["time"] != "17:00" for a in again["alternatives"])
    assert "Nearest free times" in again["message"]


async def test_a_slot_inside_the_lead_time_can_still_be_booked(session_factory: Factory) -> None:
    wide = Settings(_env_file=None, clinic_sessions="00:00-23:45", clinic_open_days="0,1,2,3,4,5,6")
    sched = appts.schedule_from_settings(wide)
    now = sched.now()
    soon = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0) + timedelta(
        minutes=15
    )
    if soon.date() != now.date():
        pytest.skip("too close to midnight")
    assert soon not in await appts.free_slots(session_factory, sched, soon.date()) or (
        soon - now >= timedelta(minutes=appts.MIN_LEAD_MINUTES)
    )
    assert soon in await appts.free_slots(session_factory, sched, soon.date(), lead_minutes=0)
    outcome = await appts.book(
        session_factory,
        sched,
        name="Asha Rao",
        phone="9876543210",
        slot=soon,
        reason="",
        call_sid="",
    )
    assert outcome.ok, outcome.message


# ---------- the guard ----------


@pytest.mark.parametrize(
    "said",
    [
        "Yes, 5 PM is available.",
        "Thursday evening is free, I can fit you in.",
        "We have a slot at 10 AM tomorrow.",
        "That time is available on Friday.",
    ],
)
def test_no_promise_before_the_schedule_was_looked_at(said: str) -> None:
    blocked = check_sentence(said, booked=False, cancelled=False, checked=False)
    assert blocked == (SAFE_CHECK_FIRST, "promised a time before checking the schedule")
    assert check_sentence(said, booked=False, cancelled=False, checked=True) == (said, None)


@pytest.mark.parametrize(
    "said",
    [
        "Is 5 PM okay for you?",
        "We are open Monday to Saturday.",
        "Which day would you like to come in?",
        "5 PM is not available, sorry.",
    ],
)
def test_normal_sentences_are_never_blocked(said: str) -> None:
    assert check_sentence(said, booked=False, cancelled=False, checked=False) == (said, None)


# ---------- prompt and ready-made phrases ----------


def test_the_prompt_teaches_the_new_rules() -> None:
    prompt = build_system_prompt(BIG, SCHED.now())
    assert "ALL" in prompt and "Do NOT call check_available_slots again" in prompt
    assert "under eight words" in prompt


async def test_fixed_phrases_are_fetched_from_the_tts_server() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, content=b"\x01\x00" * 2000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        audio = await greeting._fetch(client, Settings(_env_file=None), greeting.FILLER)
    assert audio and len(audio) == 4000 and "One moment" in seen[0]
