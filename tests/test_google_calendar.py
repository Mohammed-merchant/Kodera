"""Bookings are copied to Google Calendar in the background, after the database save."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.business.appointments import Outcome, schedule_from_settings
from app.business.booking_backend import LocalBackend, make_backend
from app.core.config import Settings
from app.integrations import google_calendar as gc
from app.integrations.google_calendar import CalendarMirror
from app.integrations.google_sheet import SheetMirror

SETTINGS = Settings(
    _env_file=None,
    google_client_id="id.apps.googleusercontent.com",
    google_client_secret=SecretStr("secret"),
    google_refresh_token=SecretStr("refresh"),
)
SLOT = datetime(2026, 11, 20, 11, 45, tzinfo=UTC)  # 17:15 in India
PHONE = "+919876543210"


class FakeBackend:
    name = "local"

    def __init__(self, ok: bool) -> None:
        self.ok = ok

    async def book(self, **_: Any) -> Outcome:
        return Outcome(self.ok, "done", SLOT if self.ok else None)

    async def cancel(self, phone: str, day: date | None) -> Outcome:
        return Outcome(self.ok, "done", SLOT if self.ok else None)

    def is_closed(self, day: date) -> bool:
        return False


class Calls(list[tuple[str, str, Any]]):
    """Every request the mirror would send to Google; `answers` sets a status per method."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, int] = {}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    seen = Calls()

    async def fake_request(cfg: Any, method: str, path: str, body: Any = None) -> httpx.Response:
        seen.append((method, path, body))
        status = seen.answers.get(method, 200)
        return httpx.Response(status, request=httpx.Request(method, "https://x"))

    monkeypatch.setattr(gc, "_request", fake_request)
    return seen


def mirror(ok: bool) -> CalendarMirror:
    return CalendarMirror(FakeBackend(ok), SETTINGS, schedule_from_settings(SETTINGS))


async def test_a_booking_becomes_a_calendar_event(calls: Calls) -> None:
    out = await mirror(True).book(
        name=" Asha Rao ", phone=PHONE, slot=SLOT, reason="fever", call_sid="CA1"
    )
    await asyncio.sleep(0)
    assert out.ok and len(calls) == 1
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/events")
    assert body["summary"] == "Appointment: Asha Rao" and "fever" in body["description"]
    assert body["start"] == {"dateTime": "2026-11-20T17:15:00+05:30", "timeZone": "Asia/Kolkata"}
    assert body["end"]["dateTime"] == "2026-11-20T17:30:00+05:30"  # one 15-minute slot


async def test_a_cancel_deletes_the_same_event(calls: Calls) -> None:
    await mirror(True).book(name="A", phone=PHONE, slot=SLOT, reason="", call_sid="")
    await mirror(True).cancel(PHONE, None)
    await asyncio.sleep(0)
    booked_id = calls[0][2]["id"]
    assert calls[1] == ("DELETE", f"/events/{booked_id}", None)


async def test_failed_bookings_are_not_copied(calls: Calls) -> None:
    await mirror(False).book(name="A", phone=PHONE, slot=SLOT, reason="", call_sid="")
    await mirror(False).cancel(PHONE, None)
    await asyncio.sleep(0)
    assert calls == []


async def test_rebooking_a_cancelled_slot_restores_the_event(calls: Calls) -> None:
    calls.answers["POST"] = 409  # Google keeps deleted event ids
    body = mirror(True).event(name="A", phone=PHONE, slot=SLOT, reason="", call_sid="")
    assert await gc.put_event(mirror(True).cfg, body) is True
    assert [c[:2] for c in calls] == [("POST", "/events"), ("PUT", f"/events/{body['id']}")]


async def test_an_event_already_gone_counts_as_deleted(calls: Calls) -> None:
    calls.answers["DELETE"] = 410
    assert await gc.delete_event(mirror(True).cfg, "kodera1") is True


def test_event_ids_are_stable_and_allowed_by_google() -> None:
    a = gc.event_id(PHONE, SLOT)
    assert a == gc.event_id(PHONE, SLOT.astimezone(schedule_from_settings(SETTINGS).tz))
    assert a != gc.event_id("+919000000000", SLOT)
    assert set(a) <= set("abcdefghijklmnopqrstuv0123456789") and 5 <= len(a) <= 1024


async def test_a_revoked_token_stops_without_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    tries = 0

    async def revoked() -> bool:
        nonlocal tries
        tries += 1
        raise gc.CalendarAuthError("invalid_grant")

    assert await gc.with_retries("booked", PHONE, revoked) is False and tries == 1


async def test_the_access_token_is_reused_until_it_expires() -> None:
    gc._tokens.clear()
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, json={"access_token": f"tok{hits}", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cfg = mirror(True).cfg
        assert await gc.access_token(client, cfg) == "tok1"
        assert await gc.access_token(client, cfg) == "tok1" and hits == 1


def test_the_calendar_is_off_without_a_refresh_token() -> None:
    assert isinstance(make_backend(Settings(_env_file=None), None), LocalBackend)  # type: ignore[arg-type]
    both = make_backend(SETTINGS.model_copy(update={"google_sheet_url": "https://s/exec"}), None)  # type: ignore[arg-type]
    assert isinstance(both, CalendarMirror) and isinstance(both.inner, SheetMirror)
    assert mirror(True).is_closed(date(2026, 11, 20)) is False  # other calls pass through
