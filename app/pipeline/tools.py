"""Tools the voice agent can call during a call: check slots, book, cancel, list.

Each tool answers with a small dict. The `message` field is plain words the agent can say.
The agent must only confirm a booking after the tool returned ok=True.
"""

import difflib
import functools
import inspect
import json
import re
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.business.booking_backend import BookingBackend, DoctorNeeded, make_backend
from app.core.config import Settings
from app.core.privacy import mask_number

log = structlog.get_logger(__name__)

MAX_GROUP_BOOKINGS = 10  # one call books at most this many (a family, not a whole school)


class GroupBooking(BaseModel):
    """One patient in a book_appointments call."""

    patient_name: str = Field(description="The patient's name, as the caller said it.")
    date: str = Field(description="The day as YYYY-MM-DD, or the word today or tomorrow.")
    time: str = Field(description="The slot time as returned by check_available_slots, like 17:15.")
    reason: str = Field(description="Why this patient wants to see the doctor, in a few words.")
    doctor: str = Field("", description="Optional. Doctor or specialty (Dr. Chen, heart).")


def build_tools(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    *,
    caller_number: str,
    call_sid: str,
    recorder: Any = None,
    backend: BookingBackend | None = None,
) -> list[Any]:
    """The tools as LiveKit function tools. Each returns JSON text for the model to read."""
    # Imported here so the rest of the app does not load LiveKit.
    from livekit.agents import function_tool

    sched = appts.schedule_from_settings(settings)
    backend = backend or make_backend(settings, factory)
    caller_phone = appts.normalize_phone(caller_number)

    def bad(message: str) -> dict[str, Any]:
        return {"ok": False, "message": message}

    def name_was_heard(name: str) -> bool:
        """Did the caller actually say this name? A small model likes to invent one ("Rajesh").
        Spelling from speech-to-text varies, so a close match is enough. Names written in another
        script than what the caller spoke cannot be compared, so those pass."""
        if recorder is None or not hasattr(recorder, "caller_words"):
            return True
        heard: set[str] = recorder.caller_words
        tokens = [t for t in re.findall(r"\w+", name.lower()) if len(t) >= 2]
        if not tokens or not heard:
            return False
        spoken = [h for h in heard if h.isascii() == tokens[0].isascii()]
        if not spoken:
            return True  # e.g. caller spoke Hindi, name written in English letters
        return any(difflib.get_close_matches(t, spoken, n=1, cutoff=0.6) for t in tokens)

    def past_day(day: Any) -> dict[str, Any]:
        today = sched.now()
        return bad(
            f"{day:%A} {day.day} {day:%B} has already passed. Today is {today:%A} {today.day} "
            f"{today:%B}. Ask the caller which day they mean, and use the CALENDAR dates."
        )

    def guarded(fn: Any) -> Any:
        """Make a LiveKit tool. If it breaks (database down...), tell the agent so it never
        invents an answer."""

        allowed = set(inspect.signature(fn).parameters)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                # Small models sometimes add arguments the tool does not have; ignore them.
                extra = set(kwargs) - allowed
                if extra:
                    log.warning("tool_extra_arguments", tool=fn.__name__, ignored=sorted(extra))
                result = await fn(*args, **{k: v for k, v in kwargs.items() if k in allowed})
            except Exception:
                log.exception("appointment_tool_failed", tool=fn.__name__, call_sid=call_sid)
                result = bad(
                    "The booking system is not working right now. Say sorry, say the clinic "
                    "staff will call back to confirm, and do not promise any booking."
                )
            return json.dumps(result, ensure_ascii=False)

        return function_tool(wrapper)

    @guarded
    async def check_available_slots(
        date: str, time: str = "", doctor: str = ""
    ) -> dict[str, Any]:
        """Look up ALL the free appointment times of one day, or check one specific time.

        Args:
            date: The day as YYYY-MM-DD, or the word today or tomorrow.
            time: Optional. A time the caller asked for (17:00 or 5 PM), to check just that one.
            doctor: Optional. The doctor or specialty asked for (Dr. Chen, heart...).
        """
        day = appts.parse_day(date, sched)
        if day is None:
            return bad("Ask the caller for a clear date.")
        if day < sched.now().date():
            return past_day(day)
        try:
            free = await backend.free_slots(day, doctor=doctor)
        except DoctorNeeded as exc:
            return bad(str(exc))
        if recorder is not None:
            recorder.checked = True  # the agent has looked: it may now talk about availability
        if not free:
            closed = backend.is_closed(day)
            msg = "The clinic is closed that day." if closed else "No free slots left that day."
            return bad(msg)
        result: dict[str, Any] = {
            "ok": True,
            "date": day.isoformat(),
            "weekday": f"{day:%A}",
            "slot_minutes": sched.slot_minutes,
            # Everything is returned, so the agent can answer "is 5 PM free?" without guessing.
            "free_times": [f"{x:%H:%M}" for x in free],
            "free_ranges_to_say": appts.spoken_ranges(free, sched),
            "note": "These are ALL the free times that day. Any time not in the list is taken.",
        }
        clock = appts.parse_clock(time) if time else None
        if clock is not None:
            wanted = datetime.combine(day, clock, tzinfo=sched.tz)
            label = appts.clock_label(wanted, sched)
            near = [
                appts.clock_label(x, sched) for x in appts.nearest(free, wanted, 3) if x != wanted
            ]
            if wanted in free:
                answer = f"{label} on {day:%A} IS FREE. Ask for the patient's name and the reason."
            else:
                answer = f"{label} on {day:%A} is NOT free. Nearest free times: {', '.join(near)}."
            # A short, plain answer first: small models misread long lists.
            result = {"ok": True, "date": day.isoformat(), "time": f"{clock:%H:%M}",
                      "time_is_free": wanted in free, "answer": answer}  # fmt: skip
        return result

    async def book_one(
        patient_name: str,
        date: str,
        time: str,
        reason: str,
        phone_number: str = "",
        doctor: str = "",
    ) -> dict[str, Any]:
        """One booking, with every check. Used by book_appointment and book_appointments."""
        day = appts.parse_day(date, sched)
        clock = appts.parse_clock(time)
        # The caller's own number (from the call) wins: models invent numbers like 1234567890.
        phone = caller_phone or appts.normalize_phone(phone_number)
        if day is None or clock is None:
            return bad("The date or time was not clear. Ask again.")
        if day < sched.now().date():
            return past_day(day)
        if phone is None:
            return bad("Ask the caller for their phone number.")
        if len(patient_name.strip()) < 2 or not name_was_heard(patient_name):
            return bad(
                "The caller has not told you the patient's name yet. Ask: May I have your "
                "name? Wait for the answer. Never invent a name."
            )
        slot = datetime.combine(day, clock, tzinfo=sched.tz)
        try:
            outcome = await backend.book(
                name=patient_name,
                phone=phone,
                slot=slot,
                reason=reason,
                call_sid=call_sid,
                doctor=doctor,
            )
        except DoctorNeeded as exc:
            return bad(str(exc))
        log.info(
            "appointment_booking",
            call_sid=call_sid,
            ok=outcome.ok,
            patient_phone=mask_number(phone),
        )
        if recorder is not None:
            recorder.booked = recorder.booked or outcome.ok  # lets the reply guard allow "booked"
            await recorder.add_event(
                "booking",
                ok=outcome.ok,
                patient=patient_name.strip()[:100],
                slot=outcome.slot.isoformat() if outcome.slot else None,
                message=outcome.message,
            )
        if outcome.ok:
            return {"ok": True, "message": outcome.message}
        # Taken (or gone): hand back the nearest free times now, so the agent can answer at once
        # instead of looking the day up again, which costs the caller several more seconds.
        alternatives: list[dict[str, str]] = []
        try:
            free = await backend.free_slots(day, doctor=doctor)
            alternatives = [
                {"time": f"{x:%H:%M}", "say": appts.clock_label(x, sched)}
                for x in appts.nearest(free, slot, 3)
            ]
        except Exception:
            log.warning("alternatives_unavailable", call_sid=call_sid)
        message = outcome.message
        if alternatives:
            message += " Nearest free times: " + ", ".join(a["say"] for a in alternatives) + "."
        refusal = {"ok": False, "message": message, "alternatives": alternatives}
        return refusal

    @guarded
    async def book_appointment(
        patient_name: str,
        date: str,
        time: str,
        reason: str,
        phone_number: str = "",
        doctor: str = "",
    ) -> dict[str, Any]:
        """Book an appointment. Only call this after the caller agreed to a free slot.

        Args:
            patient_name: The patient's name.
            date: The day as YYYY-MM-DD, or the word today or tomorrow.
            time: The slot time exactly as returned by check_available_slots, like 17:15.
            reason: Why the patient wants to see the doctor, in a few words.
            phone_number: Only if the caller's own number is unknown and they gave another one.
            doctor: Optional. The doctor or specialty asked for (Dr. Chen, heart...).
        """
        return await book_one(patient_name, date, time, reason, phone_number, doctor)

    @guarded
    async def book_appointments(bookings: list[GroupBooking]) -> dict[str, Any]:
        """Book SEVERAL appointments at once (a family, friends), in ONE call. Use it as soon as
        the caller has chosen a time for each person; do not ask "shall I book" first.

        Args:
            bookings: One entry per patient, each with patient_name, date, time and reason.
        """
        if not bookings:
            return bad("No bookings were given. Ask who needs an appointment and when.")
        results: list[dict[str, Any]] = []
        for raw in bookings[:MAX_GROUP_BOOKINGS]:
            # The model's JSON arrives as dicts (or ready objects): check each entry on its own,
            # so one incomplete entry never stops the others.
            try:
                b = raw if isinstance(raw, GroupBooking) else GroupBooking.model_validate(raw)
            except ValueError:
                who = str(raw.get("patient_name", "?")) if isinstance(raw, dict) else "?"
                results.append({"patient": who[:100], **bad("Name, day, time or reason missing.")})
                continue
            result = await book_one(b.patient_name, b.date, b.time, b.reason, doctor=b.doctor)
            results.append({"patient": b.patient_name.strip()[:100], **result})
        done = [r["patient"] for r in results if r["ok"]]
        missed = [r for r in results if not r["ok"]]
        message = f"{len(done)} of {len(results)} booked"
        message += f" ({', '.join(done)})." if done else "."
        if missed:
            message += " NOT booked: " + " ".join(f"{r['patient']}: {r['message']}" for r in missed)
        return {"ok": not missed, "booked": len(done), "results": results, "message": message}

    @guarded
    async def cancel_appointment(date: str = "") -> dict[str, Any]:
        """Cancel the caller's upcoming appointment. To reschedule, cancel then book a new one.

        Args:
            date: The day of the appointment (YYYY-MM-DD, today or tomorrow). Optional.
        """
        if caller_phone is None:
            return bad("Ask the caller for their phone number.")
        day = appts.parse_day(date, sched) if date else None
        outcome = await backend.cancel(caller_phone, day)
        log.info("appointment_cancel", call_sid=call_sid, ok=outcome.ok)
        if recorder is not None:
            recorder.cancelled = recorder.cancelled or outcome.ok
            await recorder.add_event("cancel", ok=outcome.ok, message=outcome.message)
        return {"ok": outcome.ok, "message": outcome.message}

    @guarded
    async def my_appointments() -> dict[str, Any]:
        """List the caller's upcoming appointments."""
        if caller_phone is None:
            return bad("Ask the caller for their phone number.")
        mine = await backend.upcoming(caller_phone)
        said = [appts.speak_slot(when, sched) for when in mine]
        return {"ok": True, "appointments": said or "none"}  # "none" reads better than []

    return [
        check_available_slots,
        book_appointment,
        book_appointments,
        cancel_appointment,
        my_appointments,
    ]
