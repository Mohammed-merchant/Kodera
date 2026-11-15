"""Bookings are copied to the Google Sheet in the background, after the database save."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from app.business.appointments import Outcome, schedule_from_settings
from app.business.booking_backend import LocalBackend, make_backend
from app.core.config import Settings
from app.integrations import google_sheet
from app.integrations.google_sheet import SheetMirror

SETTINGS = Settings(
    _env_file=None,
    google_sheet_url="https://script.google.com/macros/s/X/exec",
    google_sheet_secret=SecretStr("s3cret"),
)
SLOT = datetime(2026, 11, 20, 11, 45, tzinfo=UTC)  # 17:15 in India


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


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def fake_send(url: str, secret: str, row: dict[str, Any]) -> bool:
        assert url.endswith("/exec") and secret == "s3cret"
        rows.append(row)
        return True

    monkeypatch.setattr(google_sheet, "send_row", fake_send)
    return rows


def mirror(ok: bool) -> SheetMirror:
    return SheetMirror(FakeBackend(ok), SETTINGS, schedule_from_settings(SETTINGS))


async def test_a_booking_is_copied_to_the_sheet(sent: list[dict[str, Any]]) -> None:
    out = await mirror(True).book(
        name=" Asha Rao ", phone="+919876543210", slot=SLOT, reason="fever", call_sid="CA1"
    )
    await asyncio.sleep(0)
    assert out.ok and sent == [
        {"action": "booked", "phone": "+919876543210", "date": "2026-11-20", "time": "17:15",
         "patient_name": "Asha Rao", "reason": "fever", "call_id": "CA1"}
    ]  # fmt: skip


async def test_a_cancel_is_copied_and_failures_are_not(sent: list[dict[str, Any]]) -> None:
    await mirror(True).cancel("+919876543210", None)
    await mirror(False).book(name="A", phone="1", slot=SLOT, reason="", call_sid="")
    await asyncio.sleep(0)
    assert [r["action"] for r in sent] == ["cancelled"]


def test_the_sheet_is_off_without_a_url() -> None:
    s = Settings(_env_file=None)
    assert isinstance(make_backend(s, None), LocalBackend)  # type: ignore[arg-type]
    assert isinstance(make_backend(SETTINGS, None), SheetMirror)  # type: ignore[arg-type]
    assert mirror(True).is_closed(date(2026, 11, 20)) is False  # other calls pass through
