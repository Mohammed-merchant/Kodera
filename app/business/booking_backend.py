"""Where appointments live. The voice tools talk to a BookingBackend and do not care which.

    LocalBackend  our own `appointments` table (the default)
    CalBackend    self-hosted Cal.diy is the source of truth for free slots and bookings;
                  every booking is mirrored in our table so the dashboard shows everything
    HmsBackend    the Smart Hospital Management System (several doctors); mirrored the same way

Pick with BOOKING_BACKEND=local|calcom|hms. `doctor` (a name, specialty or id the caller said)
only matters for HmsBackend; the one-doctor backends ignore it.
"""

import asyncio
from datetime import date, datetime
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.business.appointments import MIN_LEAD_MINUTES, Outcome, Schedule
from app.business.calcom_sync import mirror_booking, mirror_cancel
from app.core.config import Settings
from app.integrations.calcom import CalClient, CalError
from app.integrations.hms import Doctor, HmsClient, HmsError

log = structlog.get_logger(__name__)
Factory = async_sessionmaker[AsyncSession]


class BookingBackend(Protocol):
    name: str

    def is_closed(self, day: date) -> bool: ...

    async def free_slots(
        self, day: date, lead_minutes: int | None = None, doctor: str = ""
    ) -> list[datetime]: ...

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str, doctor: str = ""
    ) -> Outcome: ...

    async def upcoming(self, phone: str) -> list[datetime]: ...

    async def cancel(self, phone: str, day: date | None) -> Outcome: ...


class LocalBackend:
    name = "local"

    def __init__(self, factory: Factory, sched: Schedule) -> None:
        self.factory, self.sched = factory, sched

    def is_closed(self, day: date) -> bool:
        return day.weekday() not in self.sched.open_days

    async def free_slots(
        self, day: date, lead_minutes: int | None = None, doctor: str = ""
    ) -> list[datetime]:
        return await appts.free_slots(self.factory, self.sched, day, lead_minutes)

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str, doctor: str = ""
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

    async def free_slots(
        self, day: date, lead_minutes: int | None = None, doctor: str = ""
    ) -> list[datetime]:
        lead = MIN_LEAD_MINUTES if lead_minutes is None else lead_minutes
        earliest = self.sched.now().timestamp() + lead * 60
        slots = await self.client.slots(day)
        return [
            s.astimezone(self.sched.tz)
            for s in slots
            if s.astimezone(self.sched.tz).date() == day and s.timestamp() >= earliest
        ]

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str, doctor: str = ""
    ) -> Outcome:
        day = slot.astimezone(self.sched.tz).date()
        if slot not in await self.free_slots(day, lead_minutes=0):  # keeps us on Cal.diy's grid
            return Outcome(False, "That time is not available.")
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


class DoctorNeeded(ValueError):
    """The caller named no doctor (or one that does not exist): the agent must ask which one."""


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("dr.", " ").replace("dr ", " ").split())


# Words callers use for a specialty -> words in the HMS specialization/department names.
_SPECIALTY_WORDS = {
    "heart": "cardio", "cardiac": "cardio", "skin": "dermat", "child": "pediatr",
    "children": "pediatr", "kids": "pediatr", "baby": "pediatr", "bone": "ortho",
    "joint": "ortho", "fracture": "ortho",
    "brain": "neuro", "nerve": "neuro", "ear": "ent", "nose": "ent", "throat": "ent",
    "eye": "ophthal", "women": "gyn", "pregnan": "gyn", "general": "general", "fever": "general",
}  # fmt: skip


def pick_doctor(doctors: list[Doctor], wanted: str, default: str = "") -> Doctor:
    """The doctor the caller asked for: by id, by name ("Dr. Chen"), or by specialty ("heart
    doctor", "cardiology"). Falls back to the default doctor, or the only one; else asks."""
    for text in (wanted, default):
        key = _norm(text)
        if not key:
            continue
        for d in doctors:
            if key == d.id.lower() or key in _norm(d.name) or _norm(d.name) in key:
                return d
        words = [w for w in key.split() if len(w) > 2]
        for d in doctors:
            area = f"{d.specialization} {d.department}".lower()
            if any(w in area for w in words) or any(
                w.startswith(k) and v in area for w in words for k, v in _SPECIALTY_WORDS.items()
            ):
                return d
    if len(doctors) == 1:
        return doctors[0]
    names = "; ".join(d.describe() for d in doctors)
    raise DoctorNeeded(f"Ask the caller which doctor or specialty they want. Doctors: {names}.")


class HmsBackend:
    """The Smart Hospital Management System holds doctors, free slots and bookings; our table
    keeps a copy of each phone booking (dashboard, my_appointments, cancel by phone)."""

    name = "hms"

    def __init__(
        self,
        client: HmsClient,
        factory: Factory,
        sched: Schedule,
        default_doctor: str,
        country_code: str = "+91",
    ) -> None:
        self.client, self.factory, self.sched = client, factory, sched
        self.default_doctor, self.country_code = default_doctor, country_code

    def is_closed(self, day: date) -> bool:
        return False  # each doctor's schedule is in the HMS; "no slots" is the only signal

    async def doctor(self, wanted: str = "") -> Doctor:
        try:
            doctors = await self.client.doctors()
        except HmsError as exc:
            raise RuntimeError(f"HMS doctors unavailable: {exc}") from exc
        return pick_doctor(doctors, wanted, self.default_doctor)

    async def free_slots(
        self, day: date, lead_minutes: int | None = None, doctor: str = ""
    ) -> list[datetime]:
        doc = await self.doctor(doctor)
        lead = MIN_LEAD_MINUTES if lead_minutes is None else lead_minutes
        earliest = self.sched.now().timestamp() + lead * 60
        times = await self.client.free_slots(doc.id, day)
        slots = [datetime.combine(day, t, tzinfo=self.sched.tz) for t in times]
        return [s for s in slots if s.timestamp() >= earliest]

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str, doctor: str = ""
    ) -> Outcome:
        doc = await self.doctor(doctor)
        local = slot.astimezone(self.sched.tz)
        try:
            patient = await self.client.patient_id(name, f"{self.country_code}{phone}")
            appt_id = await self.client.book(
                doc, local.date(), local.time(), reason=reason, patient_id=patient,
                note="Booked by phone (Kodera AI receptionist).",
            )  # fmt: skip
        except HmsError as exc:
            if exc.rejected:  # taken, or the HMS refused the data
                log.warning("hms_booking_refused", status=exc.status, error=str(exc)[:200])
                return Outcome(False, f"That time is not available with {doc.name}.")
            raise  # HMS down: the tool tells the agent not to promise anything
        await mirror_booking(
            self.factory, uid=f"hms:{appt_id}", name=name, phone=phone, reason=reason,
            start=slot, status="booked", call_sid=call_sid, source="hms", doctor=doc.name,
        )  # fmt: skip
        when = appts.speak_slot(slot, self.sched)
        return Outcome(True, f"Booked for {name.strip()} with {doc.name} on {when}.", slot)

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
        if appt.external_id and appt.external_id.startswith("hms:"):
            try:
                await self.client.cancel(appt.external_id.removeprefix("hms:"))
            except HmsError as exc:
                if exc.status != 404:  # 404 = already gone in the HMS: just fix our copy
                    raise
        await mirror_cancel(self.factory, appt.id)
        return Outcome(
            True, f"Cancelled the appointment on {appts.speak_slot(when, self.sched)}.", when
        )


_hms_clients: dict[tuple[str, str], HmsClient] = {}


def hms_client(settings: Settings) -> HmsClient:
    """One HMS client per account, shared by all calls (it keeps the login token)."""
    key = (settings.hms_api_url, settings.hms_email)
    if key not in _hms_clients:
        _hms_clients[key] = HmsClient(
            settings.hms_api_url, settings.hms_email, settings.hms_password.get_secret_value()
        )
    return _hms_clients[key]


_cal_clients: dict[tuple[str, str, int], CalClient] = {}


async def doctor_list(settings: Settings) -> list[str]:
    """The doctors for the system prompt (Smart Hospital backend only; cached, so usually
    instant). Empty if there is no list: the tools then ask which doctor."""
    if settings.booking_backend != "hms" or not settings.hms_api_url:
        return []
    try:
        doctors = await asyncio.wait_for(hms_client(settings).doctors(), 4.0)
    except Exception as exc:  # never delay or break a call for this
        log.warning("hms_doctors_unavailable", error=type(exc).__name__)
        return []
    return [d.describe() for d in doctors]


async def keep_hms_warm(settings: Settings, every_seconds: int = 540) -> None:
    """Refresh the doctor list every 9 minutes. It also keeps a free Render server awake (it
    sleeps after 15 idle minutes, and waking it takes 30-60 s, far too long for a caller)."""
    while True:
        try:
            await hms_client(settings).doctors(refresh=True)
        except Exception as exc:
            log.warning("hms_keep_warm_failed", error=type(exc).__name__)
        await asyncio.sleep(every_seconds)


def make_backend(settings: Settings, factory: Factory) -> BookingBackend:
    """The backend chosen by BOOKING_BACKEND, plus the Google Sheet and Google Calendar copies
    when they are set up. Each copy is made in the background after the database save."""
    backend = _make_backend(settings, factory)
    sched = appts.schedule_from_settings(settings)
    if settings.google_sheet_url:
        from app.integrations.google_sheet import SheetMirror

        backend = SheetMirror(backend, settings, sched)
    if settings.google_refresh_token.get_secret_value():
        from app.integrations.google_calendar import CalendarMirror

        backend = CalendarMirror(backend, settings, sched)
    return backend


def _make_backend(settings: Settings, factory: Factory) -> BookingBackend:
    """The backend chosen by BOOKING_BACKEND. Cal.diy clients are shared between calls."""
    sched = appts.schedule_from_settings(settings)
    if settings.booking_backend == "hms":
        password = settings.hms_password.get_secret_value()
        if not (settings.hms_api_url and settings.hms_email and password):
            raise ValueError("BOOKING_BACKEND=hms needs HMS_API_URL, HMS_EMAIL and HMS_PASSWORD")
        return HmsBackend(
            hms_client(settings), factory, sched, settings.hms_default_doctor,
            settings.clinic_phone_country_code,
        )  # fmt: skip
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
