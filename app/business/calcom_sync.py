"""Keep our `appointments` table in step with Cal.diy.

Two ways a Cal.diy booking reaches us:
  1. the voice agent made it (mirror_booking, right after Cal.diy confirmed it), or
  2. someone made, moved or cancelled it in Cal.diy itself (webhook -> handle_event).

Both write the same row (keyed by Cal.diy's booking uid), so doing both is harmless.
"""

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business.appointments import normalize_phone
from app.db.models import Appointment

log = structlog.get_logger(__name__)
Factory = async_sessionmaker[AsyncSession]

CANCELLED_STATUSES = {"cancelled", "canceled", "rejected"}


def signature_is_valid(secret: str, body: bytes, header: str | None) -> bool:
    """Cal.diy signs the raw body: X-Cal-Signature-256 = hex HMAC-SHA256(secret, body)."""
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _status(raw: str | None) -> str:
    return "cancelled" if (raw or "").lower() in CANCELLED_STATUSES else "booked"


async def mirror_booking(
    factory: Factory,
    *,
    uid: str,
    name: str,
    phone: str,
    reason: str,
    start: datetime,
    status: str = "booked",
    call_sid: str | None = None,
) -> bool:
    """Create or update our copy of a Cal.diy booking. Returns False if it could not be saved."""
    async with factory() as session:
        appt = await session.scalar(select(Appointment).where(Appointment.external_id == uid))
        if appt is None:
            appt = Appointment(external_id=uid, source="calcom")
            session.add(appt)
        appt.patient_name = (name or appt.patient_name or "Unknown").strip()[:100]
        appt.phone = phone or appt.phone or ""
        appt.reason = (reason or appt.reason or "").strip()[:300] or None
        appt.slot_start = start.astimezone(UTC)
        appt.status = status
        if call_sid and not appt.booked_via_call:
            appt.booked_via_call = call_sid
        try:
            await session.commit()
        except IntegrityError:  # another booking already holds this slot in our table
            await session.rollback()
            log.error("calcom_mirror_conflict", uid=uid, start=start.isoformat())
            return False
    return True


async def mirror_cancel(factory: Factory, appointment_id: int) -> None:
    async with factory() as session:
        appt = await session.get(Appointment, appointment_id)
        if appt is not None and appt.status != "cancelled":
            appt.status = "cancelled"
            await session.commit()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def handle_event(factory: Factory, event: dict[str, Any]) -> str:
    """Apply one Cal.diy webhook (BOOKING_CREATED / BOOKING_RESCHEDULED / BOOKING_CANCELLED)."""
    trigger = str(event.get("triggerEvent", ""))
    data = event.get("payload") or {}
    uid = str(data.get("uid") or "")
    if trigger not in {"BOOKING_CREATED", "BOOKING_RESCHEDULED", "BOOKING_CANCELLED"} or not uid:
        return "ignored"

    attendee = (data.get("attendees") or [{}])[0]
    metadata = data.get("metadata") or {}
    phone = (
        normalize_phone(str(attendee.get("phoneNumber") or ""))
        or normalize_phone(str(metadata.get("phone") or ""))
        or ""
    )

    if trigger == "BOOKING_CANCELLED":
        async with factory() as session:
            appt = await session.scalar(select(Appointment).where(Appointment.external_id == uid))
            if appt is None:
                return "ignored"  # we never knew this booking
            appt.status = "cancelled"
            await session.commit()
        return "cancelled"

    if trigger == "BOOKING_RESCHEDULED" and data.get("rescheduleUid"):
        async with factory() as session:  # the old booking is replaced by this new one
            old = await session.scalar(
                select(Appointment).where(Appointment.external_id == str(data["rescheduleUid"]))
            )
            if old is not None:
                old.status = "cancelled"
                await session.commit()

    saved = await mirror_booking(
        factory,
        uid=uid,
        name=str(attendee.get("name") or ""),
        phone=phone,
        reason=str(data.get("additionalNotes") or ""),
        start=_parse_time(str(data["startTime"])),
        status=_status(str(data.get("status") or "ACCEPTED")),
    )
    return (
        ("rescheduled" if trigger == "BOOKING_RESCHEDULED" else "created") if saved else "conflict"
    )
