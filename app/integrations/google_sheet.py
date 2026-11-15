"""Copies every phone booking (and cancellation) into a Google Sheet.

The sheet has a small Google Apps Script web app (docs/google_sheet_apps_script.gs) that
receives a JSON POST and writes the row. No Google Cloud project or service account is needed.

The database stays the source of truth: the sheet is updated in the background AFTER the
booking is saved, so a slow or broken Google never delays the caller or loses a booking.
Off when GOOGLE_SHEET_URL is empty.
"""

import asyncio
from datetime import date, datetime
from typing import Any

import httpx
import structlog

from app.business.appointments import Outcome, Schedule
from app.core.config import Settings
from app.core.privacy import mask_number

log = structlog.get_logger(__name__)

RETRY_DELAYS = (0, 5, 30)  # seconds before each try
_tasks: set[asyncio.Task[bool]] = set()  # keeps background sends alive until they finish


async def send_row(url: str, secret: str, row: dict[str, Any]) -> bool:
    """POST one row to the Apps Script web app, with retries. True when the sheet said ok."""
    payload = {"secret": secret, **row}
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            # Apps Script answers a POST with a redirect to the result: follow it.
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.post(url, json=payload)
            body = resp.json() if resp.status_code == 200 else {}
            if body.get("ok"):
                return True
            log.warning(
                "google_sheet_refused", attempt=attempt, status=resp.status_code,
                error=str(body.get("error", resp.text[:200])),
            )  # fmt: skip
            if body.get("error") == "bad secret":
                return False  # retrying cannot fix a wrong secret
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("google_sheet_unreachable", attempt=attempt, error=type(exc).__name__)
    log.error("google_sheet_gave_up", action=row.get("action"), phone=mask_number(row["phone"]))
    return False


def _send_later(url: str, secret: str, row: dict[str, Any]) -> None:
    task = asyncio.create_task(send_row(url, secret, row))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


class SheetMirror:
    """Wraps a BookingBackend: same behaviour, plus a copy of each change in the sheet."""

    def __init__(self, inner: Any, settings: Settings, sched: Schedule) -> None:
        self.inner = inner
        self.name = inner.name
        self.url = settings.google_sheet_url
        self.secret = settings.google_sheet_secret.get_secret_value()
        self.sched = sched

    def __getattr__(self, attr: str) -> Any:  # is_closed, free_slots, upcoming...
        return getattr(self.inner, attr)

    def _row(self, action: str, phone: str, slot: datetime | None, **extra: str) -> dict[str, Any]:
        local = slot.astimezone(self.sched.tz) if slot else None
        return {
            "action": action,
            "phone": phone,
            "date": f"{local:%Y-%m-%d}" if local else "",
            "time": f"{local:%H:%M}" if local else "",
            **extra,
        }

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str
    ) -> Outcome:
        outcome: Outcome = await self.inner.book(
            name=name, phone=phone, slot=slot, reason=reason, call_sid=call_sid
        )
        if outcome.ok:
            row = self._row(
                "booked", phone, outcome.slot or slot,
                patient_name=name.strip()[:100], reason=reason.strip()[:200], call_id=call_sid,
            )  # fmt: skip
            _send_later(self.url, self.secret, row)
        return outcome

    async def cancel(self, phone: str, day: date | None) -> Outcome:
        outcome: Outcome = await self.inner.cancel(phone, day)
        if outcome.ok:
            _send_later(self.url, self.secret, self._row("cancelled", phone, outcome.slot))
        return outcome
