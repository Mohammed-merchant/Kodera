"""Clinic appointments: which slots exist, which are free, book and cancel.

The clinic has one doctor, so a slot holds one patient. Times are stored in UTC and shown in
the clinic's own clock (a fixed UTC offset, India = +05:30, so no timezone database is needed).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Appointment

MIN_LEAD_MINUTES = 15  # a slot must start at least this far ahead
_CLOCK = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Schedule:
    open_days: frozenset[int]
    sessions: tuple[tuple[time, time], ...]
    slot_minutes: int
    tz: timezone

    def now(self) -> datetime:
        return datetime.now(self.tz)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str  # plain words the agent can say or paraphrase
    slot: datetime | None = None


def schedule_from_settings(s: Settings) -> Schedule:
    sessions = []
    for part in s.clinic_sessions.split(","):
        start, _, end = part.strip().partition("-")
        sessions.append((time.fromisoformat(start.strip()), time.fromisoformat(end.strip())))
    return Schedule(
        open_days=frozenset(int(d) for d in s.clinic_open_days.split(",") if d.strip()),
        sessions=tuple(sessions),
        slot_minutes=s.clinic_slot_minutes,
        tz=timezone(timedelta(minutes=s.clinic_utc_offset_minutes)),
    )


# ---------- reading what the caller said ----------


def parse_day(text: str, sched: Schedule) -> date | None:
    """'today', 'tomorrow' or YYYY-MM-DD."""
    word = text.strip().lower()
    today = sched.now().date()
    if word == "today":
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(word)
    except ValueError:
        return None


def parse_clock(text: str) -> time | None:
    """'17:30', '5:30 pm', '5 pm' -> time."""
    m = _CLOCK.match(text)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if meridiem.startswith("p") and hour < 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def normalize_phone(raw: str) -> str | None:
    """Digits only, last 10 (drops +91 / 0). None if it is not a usable number."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else None


# ---------- saying it back ----------


def speak_slot(slot: datetime, sched: Schedule) -> str:
    local = slot.astimezone(sched.tz)
    hour12 = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    clock = f"{hour12}:{local.minute:02d} {suffix}" if local.minute else f"{hour12} {suffix}"
    return f"{local:%A} {local.day} {local:%B} at {clock}"


def describe_hours(sched: Schedule, s: Settings) -> str:
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days = ", ".join(names[d] for d in sorted(sched.open_days))
    sessions = " and ".join(f"{a:%H:%M} to {b:%H:%M}" for a, b in sched.sessions)
    return f"{days}, {sessions} (closed on other days). Slots are {s.clinic_slot_minutes} minutes."


# ---------- slots ----------


def slots_on(sched: Schedule, day: date) -> list[datetime]:
    """Every consultation slot of a day, in clinic time (empty on a closed day)."""
    if day.weekday() not in sched.open_days:
        return []
    out: list[datetime] = []
    step = timedelta(minutes=sched.slot_minutes)
    for start, end in sched.sessions:
        cursor = datetime.combine(day, start, tzinfo=sched.tz)
        last = datetime.combine(day, end, tzinfo=sched.tz)
        while cursor + step <= last:
            out.append(cursor)
            cursor += step
    return out


def as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive datetimes


async def _booked_between(session: AsyncSession, start: datetime, end: datetime) -> set[datetime]:
    rows = await session.scalars(
        select(Appointment.slot_start).where(
            Appointment.status == "booked",
            Appointment.slot_start >= start,
            Appointment.slot_start < end,
        )
    )
    return {as_aware(r) for r in rows}


async def free_slots(
    factory: async_sessionmaker[AsyncSession], sched: Schedule, day: date
) -> list[datetime]:
    slots = slots_on(sched, day)
    if not slots:
        return []
    earliest = sched.now() + timedelta(minutes=MIN_LEAD_MINUTES)
    async with factory() as session:
        taken = await _booked_between(session, slots[0], slots[-1] + timedelta(minutes=1))
    return [s for s in slots if s >= earliest and s.astimezone(UTC) not in taken]


# ---------- booking ----------


async def book(
    factory: async_sessionmaker[AsyncSession],
    sched: Schedule,
    *,
    name: str,
    phone: str,
    slot: datetime,
    reason: str,
    call_sid: str,
) -> Outcome:
    if slot not in await free_slots(factory, sched, slot.date()):
        return Outcome(False, "That time is not available. Please check the free slots again.")
    async with factory() as session:
        session.add(
            Appointment(
                patient_name=name.strip()[:100],
                phone=phone,
                reason=reason.strip()[:300] or None,
                slot_start=slot.astimezone(UTC),
                booked_via_call=call_sid or None,
            )
        )
        try:
            await session.commit()
        except IntegrityError:  # someone took the slot a moment ago
            await session.rollback()
            return Outcome(False, "Sorry, that slot was just taken. Please pick another time.")
    return Outcome(True, f"Booked for {name.strip()} on {speak_slot(slot, sched)}.", slot)


async def upcoming(
    factory: async_sessionmaker[AsyncSession], sched: Schedule, phone: str
) -> list[Appointment]:
    async with factory() as session:
        rows = await session.scalars(
            select(Appointment)
            .where(
                Appointment.phone == phone,
                Appointment.status == "booked",
                Appointment.slot_start >= datetime.now(UTC),
            )
            .order_by(Appointment.slot_start)
        )
        return list(rows)


async def cancel(
    factory: async_sessionmaker[AsyncSession], sched: Schedule, phone: str, day: date | None
) -> Outcome:
    mine = await upcoming(factory, sched, phone)
    if day:
        mine = [a for a in mine if as_aware(a.slot_start).astimezone(sched.tz).date() == day]
    if not mine:
        return Outcome(False, "I could not find an upcoming appointment for this number.")
    if len(mine) > 1:
        options = " or ".join(speak_slot(as_aware(a.slot_start), sched) for a in mine)
        return Outcome(False, f"There is more than one appointment: {options}. Which one?")
    async with factory() as session:
        appt = await session.get(Appointment, mine[0].id)
        if appt is None or appt.status != "booked":
            return Outcome(False, "That appointment is already cancelled.")
        appt.status = "cancelled"
        await session.commit()
    when = as_aware(mine[0].slot_start)
    return Outcome(True, f"Cancelled the appointment on {speak_slot(when, sched)}.", when)
