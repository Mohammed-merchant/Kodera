"""Where appointments live. The voice tools talk to a BookingBackend and do not care which.

    LocalBackend  our own `appointments` table (the default)
    CalBackend    self-hosted Cal.diy is the source of truth for free slots and bookings;
                  every booking is mirrored in our table so the dashboard shows everything

Pick with BOOKING_BACKEND=local|calcom.
"""

from datetime import date, datetime
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.business.appointments import MIN_LEAD_MINUTES, Outcome, Schedule
from app.business.calcom_sync import mirror_booking, mirror_cancel
from app.core.config import Settings
from app.integrations.calcom import CalClient, CalError

log = structlog.get_logger(__name__)
Factory = async_sessionmaker[AsyncSession]


class BookingBackend(Protocol):
    name: str

    def is_closed(self, day: date) -> bool: ...

    async def free_slots(self, day: date) -> list[datetime]: ...

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str
    ) -> Outcome: ...

    async def upcoming(self, phone: str) -> list[datetime]: ...

    async def cancel(self, phone: str, day: date | None) -> Outcome: ...


class LocalBackend:
    name = "local"

    def __init__(self, factory: Factory, sched: Schedule) -> None:
        self.factory, self.sched = factory, sched

    def is_closed(self, day: date) -> bool:
        return day.weekday() not in self.sched.open_days

    async def free_slots(self, day: date) -> list[datetime]:
        return await appts.free_slots(self.factory, self.sched, day)

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str
    ) -> Outcome:
        return await appts.book(
            self.factory, self.sched, name=name, phone=phone, slot=slot, reason=reason,
            call_sid=call_sid,
        )  # fmt: skip

    async def upcoming(self, phone: str) -> list[datetime]:
        rows = await appts.upcoming(self.factory, self.sched, phone)
        return [appts.as_aware(a.slot_start) for a in rows]

    async def cancel(self, phone: str, day: date | None) -> Outcome:
        return await appts.cancel(self.factory, self.sched, phone, day)


class CalBackend:
    """Free slots and bookings come from Cal.diy; our table is a mirror for the dashboard."""

    name = "calcom"

    def __init__(
        self, client: CalClient, factory: Factory, sched: Schedule, country_code: str
    ) -> None:
        self.client, self.factory, self.sched = client, factory, sched
        self.country_code = country_code

    def is_closed(self, day: date) -> bool:
        return False  # Cal.diy knows the doctor's schedule; "no slots" is the only signal

    async def free_slots(self, day: date) -> list[datetime]:
        earliest = self.sched.now().timestamp() + MIN_LEAD_MINUTES * 60
        slots = await self.client.slots(day)
        return [
            s.astimezone(self.sched.tz)
            for s in slots
            if s.astimezone(self.sched.tz).date() == day and s.timestamp() >= earliest
        ]

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str
    ) -> Outcome:
        day = slot.astimezone(self.sched.tz).date()
        if slot not in await self.free_slots(day):  # also keeps us on Cal.diy's own slot grid
            return Outcome(False, "That time is not available. Please check the free slots again.")
        try:
            booking = await self.client.create_booking(
                start=slot,
                name=name.strip()[:100],
                phone_e164=f"{self.country_code}{phone}",
                notes=reason.strip()[:300],
                metadata={"source": "voice-agent", "callSid": call_sid[:64], "phone": phone},
            )
        except CalError as exc:
            if exc.rejected:  # someone else took it, or Cal.diy refused the data
                return Outcome(False, "Sorry, that slot was just taken. Please pick another time.")
            raise  # Cal.diy down: the tool tells the agent not to promise anything
        await mirror_booking(
            self.factory, uid=str(booking["uid"]), name=name, phone=phone, reason=reason,
            start=slot, status="booked", call_sid=call_sid,
        )  # fmt: skip
        return Outcome(
            True, f"Booked for {name.strip()} on {appts.speak_slot(slot, self.sched)}.", slot
        )

    async def upcoming(self, phone: str) -> list[datetime]:
        rows = await appts.upcoming(self.factory, self.sched, phone)
        return [appts.as_aware(a.slot_start) for a in rows]

    async def cancel(self, phone: str, day: date | None) -> Outcome:
        mine = await appts.upcoming(self.factory, self.sched, phone)
        if day:
            mine = [
                a for a in mine
                if appts.as_aware(a.slot_start).astimezone(self.sched.tz).date() == day
            ]  # fmt: skip
        if not mine:
            return Outcome(False, "I could not find an upcoming appointment for this number.")
        if len(mine) > 1:
            options = " or ".join(
                appts.speak_slot(appts.as_aware(a.slot_start), self.sched) for a in mine
            )
            return Outcome(False, f"There is more than one appointment: {options}. Which one?")
        appt = mine[0]
        when = appts.as_aware(appt.slot_start)
        if appt.external_id:  # made in Cal.diy: cancel it there first
            try:
                await self.client.cancel_booking(
                    appt.external_id, "Cancelled by the patient by phone"
                )
            except CalError as exc:
                if exc.status != 404:  # 404 = already gone in Cal.diy: just fix our copy
                    raise
        await mirror_cancel(self.factory, appt.id)
        return Outcome(
            True, f"Cancelled the appointment on {appts.speak_slot(when, self.sched)}.", when
        )


_cal_clients: dict[tuple[str, str, int], CalClient] = {}


def make_backend(settings: Settings, factory: Factory) -> BookingBackend:
    """The backend chosen by BOOKING_BACKEND. Cal.diy clients are shared between calls."""
    sched = appts.schedule_from_settings(settings)
    if settings.booking_backend != "calcom":
        return LocalBackend(factory, sched)
    key = settings.cal_api_key.get_secret_value()
    if not key or not settings.cal_event_type_id:
        raise ValueError("BOOKING_BACKEND=calcom needs CAL_API_KEY and CAL_EVENT_TYPE_ID")
    cache_key = (settings.cal_api_url, key, settings.cal_event_type_id)
    client = _cal_clients.get(cache_key)
    if client is None:
        client = _cal_clients[cache_key] = CalClient(
            base_url=settings.cal_api_url,
            api_key=key,
            event_type_id=settings.cal_event_type_id,
            timezone_name=settings.clinic_timezone_name,
        )
    return CalBackend(client, factory, sched, settings.clinic_phone_country_code)
