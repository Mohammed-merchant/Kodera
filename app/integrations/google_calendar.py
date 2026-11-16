"""Copies every phone booking (and cancellation) into Google Calendar.

Uses an OAuth client (Google Cloud > APIs & Services > Credentials) and a refresh token that
scripts/google_calendar_auth.py gets once, when the clinic's Google account clicks "Allow".
See docs/google_calendar.md.

Like the Google Sheet copy, the database stays the source of truth: the calendar is updated in
the background AFTER the booking is saved, so a slow or broken Google never delays the caller
or loses a booking. Off when GOOGLE_REFRESH_TOKEN is empty.

Each event's id is made from the phone number and the slot, so a retry never makes a second
event and a cancellation finds the event without storing anything extra in the database.
"""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from app.business.appointments import Outcome, Schedule
from app.core.config import Settings
from app.core.privacy import mask_number

log = structlog.get_logger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/calendar/v3"
RETRY_DELAYS = (0, 5, 30)  # seconds before each try
_tasks: set[asyncio.Task[bool]] = set()  # keeps background sends alive until they finish
_tokens: dict[str, tuple[str, float]] = {}  # refresh token -> (access token, expiry time)


class CalendarAuthError(Exception):
    """Google refused the refresh token (revoked or expired): retrying cannot fix it."""


@dataclass(frozen=True)
class CalendarConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    calendar_id: str
    timezone_name: str
    slot_minutes: int

    @classmethod
    def from_settings(cls, s: Settings) -> "CalendarConfig":
        return cls(
            client_id=s.google_client_id,
            client_secret=s.google_client_secret.get_secret_value(),
            refresh_token=s.google_refresh_token.get_secret_value(),
            calendar_id=s.google_calendar_id or "primary",
            timezone_name=s.clinic_timezone_name,
            slot_minutes=s.clinic_slot_minutes,
        )


def event_id(phone: str, slot: datetime) -> str:
    """The same phone and slot always give the same id (Google allows the letters a-v and 0-9)."""
    key = f"{phone}|{slot.astimezone(UTC).isoformat()}"
    return "kodera" + hashlib.sha1(key.encode()).hexdigest()


async def access_token(client: httpx.AsyncClient, cfg: CalendarConfig) -> str:
    """A short-lived access token from the refresh token, reused until a minute before expiry."""
    cached = _tokens.get(cfg.refresh_token)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    resp = await client.post(TOKEN_URL, data={
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "refresh_token": cfg.refresh_token,
        "grant_type": "refresh_token",
    })  # fmt: skip
    if resp.status_code in (400, 401):
        raise CalendarAuthError(str(resp.json().get("error", resp.status_code)))
    resp.raise_for_status()
    body = resp.json()
    _tokens[cfg.refresh_token] = (
        body["access_token"],
        time.time() + int(body.get("expires_in", 3600)),
    )
    return str(body["access_token"])


async def _request(cfg: CalendarConfig, method: str, path: str, body: Any = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=20.0) as client:
        token = await access_token(client, cfg)
        url = f"{API_URL}/calendars/{quote(cfg.calendar_id, safe='')}{path}"
        resp = await client.request(
            method, url, json=body, headers={"Authorization": f"Bearer {token}"}
        )
    if resp.status_code == 401:  # the access token stopped working early: get a new one next try
        _tokens.pop(cfg.refresh_token, None)
    return resp


def _refused(action: str, resp: httpx.Response) -> bool:
    log.warning(
        "google_calendar_refused", action=action, status=resp.status_code, error=resp.text[:200]
    )
    return False


async def put_event(cfg: CalendarConfig, body: dict[str, Any]) -> bool:
    resp = await _request(cfg, "POST", "/events", body)
    if resp.status_code == 409:
        # The id exists: this patient had this slot before and cancelled it. Restore that event.
        resp = await _request(cfg, "PUT", f"/events/{body['id']}", body)
    return resp.status_code in (200, 201) or _refused("booked", resp)


async def delete_event(cfg: CalendarConfig, eid: str) -> bool:
    resp = await _request(cfg, "DELETE", f"/events/{eid}")
    # 404/410: not in the calendar (booked before the calendar copy was on, or already deleted).
    return resp.status_code in (200, 204, 404, 410) or _refused("cancelled", resp)


async def with_retries(
    action: str, phone: str, attempt_once: Callable[[], Awaitable[bool]]
) -> bool:
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await attempt_once():
                return True
        except CalendarAuthError as exc:
            log.error(
                "google_calendar_auth_failed",
                error=str(exc),
                hint="run scripts/google_calendar_auth.py again and update GOOGLE_REFRESH_TOKEN",
            )
            return False
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("google_calendar_unreachable", attempt=attempt, error=type(exc).__name__)
    log.error("google_calendar_gave_up", action=action, phone=mask_number(phone))
    return False


def _later(action: str, phone: str, attempt_once: Callable[[], Awaitable[bool]]) -> None:
    task = asyncio.create_task(with_retries(action, phone, attempt_once))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


class CalendarMirror:
    """Wraps a BookingBackend: same behaviour, plus a calendar event for each booking."""

    def __init__(self, inner: Any, settings: Settings, sched: Schedule) -> None:
        self.inner = inner
        self.name = inner.name
        self.cfg = CalendarConfig.from_settings(settings)
        self.sched = sched

    def __getattr__(self, attr: str) -> Any:  # is_closed, free_slots, upcoming...
        return getattr(self.inner, attr)

    def event(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str
    ) -> dict[str, Any]:
        start = slot.astimezone(self.sched.tz)
        end = start + timedelta(minutes=self.cfg.slot_minutes)
        name = name.strip()[:100]
        return {
            "id": event_id(phone, slot),
            "summary": f"Appointment: {name}",
            "description": (
                f"Patient: {name}\nPhone: {phone}\nReason: {reason.strip()[:200]}\n"
                f"Booked by phone (Kodera voice agent). Call: {call_sid}"
            ),
            "start": {"dateTime": start.isoformat(), "timeZone": self.cfg.timezone_name},
            "end": {"dateTime": end.isoformat(), "timeZone": self.cfg.timezone_name},
            "status": "confirmed",
        }

    async def book(
        self, *, name: str, phone: str, slot: datetime, reason: str, call_sid: str
    ) -> Outcome:
        outcome: Outcome = await self.inner.book(
            name=name, phone=phone, slot=slot, reason=reason, call_sid=call_sid
        )
        if outcome.ok:
            body = self.event(
                name=name, phone=phone, slot=outcome.slot or slot, reason=reason, call_sid=call_sid
            )
            _later("booked", phone, lambda: put_event(self.cfg, body))
        return outcome

    async def cancel(self, phone: str, day: date | None) -> Outcome:
        outcome: Outcome = await self.inner.cancel(phone, day)
        if outcome.ok and outcome.slot:
            eid = event_id(phone, outcome.slot)
            _later("cancelled", phone, lambda: delete_event(self.cfg, eid))
        return outcome
