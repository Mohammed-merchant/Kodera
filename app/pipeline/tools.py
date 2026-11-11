"""Tools the voice agent can call during a call: check slots, book, cancel, list.

Each tool answers with a small dict. The `message` field is plain words the agent can say.
The agent must only confirm a booking after the tool returned ok=True.
"""

import functools
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.core.config import Settings
from app.core.privacy import mask_number

log = structlog.get_logger(__name__)

MAX_SLOTS_OFFERED = 6


def build_tools(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    *,
    caller_number: str,
    call_sid: str,
) -> list[Any]:
    # Imported here so the rest of the app does not load Pipecat.
    from pipecat.services.llm_service import FunctionCallParams

    sched = appts.schedule_from_settings(settings)
    caller_phone = appts.normalize_phone(caller_number)

    def bad(message: str) -> dict[str, Any]:
        return {"ok": False, "message": message}

    def guarded(fn: Any) -> Any:
        """If a tool breaks (database down...), tell the agent so it never invents an answer."""

        @functools.wraps(fn)
        async def wrapper(params: FunctionCallParams, *args: Any, **kwargs: Any) -> None:
            try:
                await fn(params, *args, **kwargs)
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
    async def check_available_slots(params: FunctionCallParams, date: str) -> None:
        """Look up the free appointment slots on one day.

        Args:
            date: The day as YYYY-MM-DD, or the word today or tomorrow.
        """
        day = appts.parse_day(date, sched)
        if day is None:
            return await params.result_callback(bad("Ask the caller for a clear date."))
        free = await appts.free_slots(factory, sched, day)
        if not free:
            closed = day.weekday() not in sched.open_days
            msg = "The clinic is closed that day." if closed else "No free slots left that day."
            return await params.result_callback(bad(msg))
        shown = free[:MAX_SLOTS_OFFERED]
        await params.result_callback(
            {
                "ok": True,
                "date": day.isoformat(),
                "free_slots": [
                    {"time": f"{s:%H:%M}", "say": appts.speak_slot(s, sched)} for s in shown
                ],
                "more_slots_available": len(free) > len(shown),
            }
        )

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
        phone = appts.normalize_phone(phone_number) or caller_phone
        if day is None or clock is None:
            return await params.result_callback(bad("The date or time was not clear. Ask again."))
        if phone is None:
            return await params.result_callback(bad("Ask the caller for their phone number."))
        if len(patient_name.strip()) < 2:
            return await params.result_callback(bad("Ask for the patient's name."))
        slot = datetime.combine(day, clock, tzinfo=sched.tz)
        outcome = await appts.book(
            factory,
            sched,
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
        await params.result_callback({"ok": outcome.ok, "message": outcome.message})

    @guarded
    async def cancel_appointment(params: FunctionCallParams, date: str = "") -> None:
        """Cancel the caller's upcoming appointment. To reschedule, cancel then book a new one.

        Args:
            date: The day of the appointment (YYYY-MM-DD, today or tomorrow). Optional.
        """
        if caller_phone is None:
            return await params.result_callback(bad("Ask the caller for their phone number."))
        day = appts.parse_day(date, sched) if date else None
        outcome = await appts.cancel(factory, sched, caller_phone, day)
        log.info("appointment_cancel", call_sid=call_sid, ok=outcome.ok)
        await params.result_callback({"ok": outcome.ok, "message": outcome.message})

    @guarded
    async def my_appointments(params: FunctionCallParams) -> None:
        """List the caller's upcoming appointments."""
        if caller_phone is None:
            return await params.result_callback(bad("Ask the caller for their phone number."))
        mine = await appts.upcoming(factory, sched, caller_phone)
        said = [appts.speak_slot(appts.as_aware(a.slot_start), sched) for a in mine]
        await params.result_callback(
            {"ok": True, "appointments": said or "none"}  # "none" reads better than []
        )

    return [check_available_slots, book_appointment, cancel_appointment, my_appointments]
