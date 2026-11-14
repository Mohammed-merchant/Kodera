"""Tools the voice agent can call during a call: check slots, book, cancel, list.

Each tool answers with a small dict. The `message` field is plain words the agent can say.
The agent must only confirm a booking after the tool returned ok=True.
"""

import difflib
import functools
import inspect
import re
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.business.booking_backend import BookingBackend, make_backend
from app.core.config import Settings
from app.core.privacy import mask_number

log = structlog.get_logger(__name__)


def build_tools(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    *,
    caller_number: str,
    call_sid: str,
    recorder: Any = None,
    backend: BookingBackend | None = None,
) -> list[Any]:
    # Imported here so the rest of the app does not load Pipecat.
    from pipecat.services.llm_service import FunctionCallParams

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
        """If a tool breaks (database down...), tell the agent so it never invents an answer."""

        allowed = set(inspect.signature(fn).parameters)

        @functools.wraps(fn)
        async def wrapper(params: FunctionCallParams, *args: Any, **kwargs: Any) -> None:
            try:
                # Small models sometimes add arguments the tool does not have; ignore them.
                extra = set(kwargs) - allowed
                if extra:
                    log.warning("tool_extra_arguments", tool=fn.__name__, ignored=sorted(extra))
                await fn(params, *args, **{k: v for k, v in kwargs.items() if k in allowed})
            except Exception:
                log.exception("appointment_tool_failed", tool=fn.__name__, call_sid=call_sid)
                await params.result_callback(
                    bad(
                        "The booking system is not working right now. Say sorry, say the clinic "
                        "staff will call back to confirm, and do not promise any booking."
                    )
                )

        return wrapper

    @guarded
    async def check_available_slots(params: FunctionCallParams, date: str, time: str = "") -> None:
        """Look up ALL the free appointment times of one day, or check one specific time.

        Args:
            date: The day as YYYY-MM-DD, or the word today or tomorrow.
            time: Optional. A time the caller asked for (17:00 or 5 PM), to check just that one.
        """
        day = appts.parse_day(date, sched)
        if day is None:
            return await params.result_callback(bad("Ask the caller for a clear date."))
        if day < sched.now().date():
            return await params.result_callback(past_day(day))
        free = await backend.free_slots(day)
        if recorder is not None:
            recorder.checked = True  # the agent has looked: it may now talk about availability
        if not free:
            closed = backend.is_closed(day)
            msg = "The clinic is closed that day." if closed else "No free slots left that day."
            return await params.result_callback(bad(msg))
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
        await params.result_callback(result)

    @guarded
    async def book_appointment(
        params: FunctionCallParams,
        patient_name: str,
        date: str,
        time: str,
        reason: str,
        phone_number: str = "",
    ) -> None:
        """Book an appointment. Only call this after the caller agreed to a free slot.

        Args:
            patient_name: The patient's name.
            date: The day as YYYY-MM-DD, or the word today or tomorrow.
            time: The slot time exactly as returned by check_available_slots, like 17:15.
            reason: Why the patient wants to see the doctor, in a few words.
            phone_number: Only if the caller's own number is unknown and they gave another one.
        """
        day = appts.parse_day(date, sched)
        clock = appts.parse_clock(time)
        # The caller's own number (from the call) wins: models invent numbers like 1234567890.
        phone = caller_phone or appts.normalize_phone(phone_number)
        if day is None or clock is None:
            return await params.result_callback(bad("The date or time was not clear. Ask again."))
        if day < sched.now().date():
            return await params.result_callback(past_day(day))
        if phone is None:
            return await params.result_callback(bad("Ask the caller for their phone number."))
        if len(patient_name.strip()) < 2 or not name_was_heard(patient_name):
            return await params.result_callback(
                bad(
                    "The caller has not told you the patient's name yet. Ask: May I have your "
                    "name? Wait for the answer. Never invent a name."
                )
            )
        slot = datetime.combine(day, clock, tzinfo=sched.tz)
        outcome = await backend.book(
            name=patient_name,
            phone=phone,
            slot=slot,
            reason=reason,
            call_sid=call_sid,
        )
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
            return await params.result_callback({"ok": True, "message": outcome.message})
        # Taken (or gone): hand back the nearest free times now, so the agent can answer at once
        # instead of looking the day up again, which costs the caller several more seconds.
        alternatives: list[dict[str, str]] = []
        try:
            free = await backend.free_slots(day)
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
        await params.result_callback(refusal)

    @guarded
    async def cancel_appointment(params: FunctionCallParams, date: str = "") -> None:
        """Cancel the caller's upcoming appointment. To reschedule, cancel then book a new one.

        Args:
            date: The day of the appointment (YYYY-MM-DD, today or tomorrow). Optional.
        """
        if caller_phone is None:
            return await params.result_callback(bad("Ask the caller for their phone number."))
        day = appts.parse_day(date, sched) if date else None
        outcome = await backend.cancel(caller_phone, day)
        log.info("appointment_cancel", call_sid=call_sid, ok=outcome.ok)
        if recorder is not None:
            recorder.cancelled = recorder.cancelled or outcome.ok
            await recorder.add_event("cancel", ok=outcome.ok, message=outcome.message)
        await params.result_callback({"ok": outcome.ok, "message": outcome.message})

    @guarded
    async def my_appointments(params: FunctionCallParams) -> None:
        """List the caller's upcoming appointments."""
        if caller_phone is None:
            return await params.result_callback(bad("Ask the caller for their phone number."))
        mine = await backend.upcoming(caller_phone)
        said = [appts.speak_slot(when, sched) for when in mine]
        await params.result_callback(
            {"ok": True, "appointments": said or "none"}  # "none" reads better than []
        )

    return [check_available_slots, book_appointment, cancel_appointment, my_appointments]
