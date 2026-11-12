"""Cal.diy integration, tested against a fake of its API v2 (formats taken from its source)."""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.business.booking_backend import CalBackend, LocalBackend, make_backend
from app.core.config import Settings, get_settings
from app.db.models import Appointment
from app.integrations.calcom import CalClient, CalError
from app.main import app
from app.pipeline.tools import build_tools
from tests.test_appointments import SCHED, FakeParams, call, next_open_day

Factory = async_sessionmaker[AsyncSession]
KEY = "cal_test_key"


class FakeCal:
    """Just enough of Cal.diy API v2: slots, create booking, cancel booking."""

    def __init__(self, day: str) -> None:
        self.day = day
        self.taken: set[str] = set()
        self.bookings: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.down = False

    def slot_starts(self) -> list[str]:  # the doctor works 10:00-10:30 and 17:00, clinic time
        return [f"{self.day}T10:00:00.000+05:30", f"{self.day}T10:15:00.000+05:30",
                f"{self.day}T17:00:00.000+05:30"]  # fmt: skip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, text="maintenance")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == f"Bearer {KEY}"
        path, version = request.url.path, request.headers.get("cal-api-version")
        if request.method == "GET" and path == "/v2/slots":
            assert version == "2024-09-04"
            free = [s for s in self.slot_starts() if _utc(s) not in self.taken]
            return httpx.Response(200, json={"status": "success", "data": {self.day: [
                {"start": s} for s in free]}})  # fmt: skip
        assert version == "2024-08-13"
        if request.method == "POST" and path == "/v2/bookings":
            start = body["start"]
            if start in self.taken:
                return httpx.Response(400, json={"status": "error", "error": {"message": "taken"}})
            self.taken.add(start)
            uid = f"uid{len(self.bookings) + 1}"
            self.bookings[uid] = {"uid": uid, "start": start, "status": "accepted", "body": body}
            return httpx.Response(201, json={"status": "success", "data": self.bookings[uid]})
        if request.method == "POST" and path.endswith("/cancel"):
            uid = path.split("/")[3]
            if uid not in self.bookings:
                return httpx.Response(404, json={"status": "error", "error": {"message": "none"}})
            self.bookings[uid]["status"] = "cancelled"
            self.taken.discard(self.bookings[uid]["start"])
            return httpx.Response(200, json={"status": "success", "data": self.bookings[uid]})
        return httpx.Response(404)


def _utc(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def settings_for_cal(**extra: object) -> Settings:
    return Settings(
        _env_file=None, booking_backend="calcom", cal_api_key=SecretStr(KEY),
        cal_event_type_id=7, cal_webhook_secret=SecretStr("whsec"), **extra,
    )  # fmt: skip


@pytest.fixture
def cal() -> FakeCal:
    return FakeCal(next_open_day().isoformat())


@pytest_asyncio.fixture
async def backend(cal: FakeCal, session_factory: Factory) -> AsyncIterator[CalBackend]:
    client = CalClient(
        base_url="http://cal",
        api_key=KEY,
        event_type_id=7,
        timezone_name="Asia/Kolkata",
        transport=httpx.MockTransport(cal),
    )
    yield CalBackend(client, session_factory, SCHED, "+91")
    await client.aclose()


def day_of(cal: FakeCal):
    return datetime.fromisoformat(cal.day).date()


def slot(cal: FakeCal, hhmm: str) -> datetime:
    clock = appts.parse_clock(hhmm)
    assert clock
    return datetime.combine(day_of(cal), clock, tzinfo=SCHED.tz)


# ---------- the API client ----------


async def test_slots_are_read_and_sent_with_the_right_headers(backend: CalBackend, cal: FakeCal):
    slots = await backend.client.slots(day_of(cal))
    assert [f"{s.astimezone(SCHED.tz):%H:%M}" for s in slots] == ["10:00", "10:15", "17:00"]
    assert all(s.tzinfo for s in slots)
    method, path, _ = cal.calls[0]
    assert (method, path) == ("GET", "/v2/slots")


async def test_a_booking_is_sent_as_cal_expects(backend: CalBackend, cal: FakeCal):
    made = await backend.client.create_booking(
        start=slot(cal, "17:00"), name="Asha Rao", phone_e164="+919876543210",
        notes="fever", metadata={"source": "voice-agent"},
    )  # fmt: skip
    body = cal.bookings[made["uid"]]["body"]
    assert body["start"].endswith("Z") and body["eventTypeId"] == 7
    assert body["attendee"] == {"name": "Asha Rao", "timeZone": "Asia/Kolkata",
                                "phoneNumber": "+919876543210", "language": "en"}  # fmt: skip
    assert body["bookingFieldsResponses"] == {"notes": "fever"}
    assert body["metadata"] == {"source": "voice-agent"}


async def test_errors_tell_a_refusal_from_an_outage(backend: CalBackend, cal: FakeCal):
    with pytest.raises(CalError) as refused:
        await backend.client.cancel_booking("nope", "x")
    assert refused.value.rejected and refused.value.status == 404
    cal.down = True
    with pytest.raises(CalError) as outage:
        await backend.client.slots(day_of(cal))
    assert not outage.value.rejected  # 503: Cal.diy is down, not "no"


# ---------- backend: booking ----------


async def test_booking_goes_to_cal_and_is_mirrored_for_the_dashboard(
    backend: CalBackend, cal: FakeCal, session_factory: Factory
):
    outcome = await backend.book(name="Asha Rao", phone="9876543210", slot=slot(cal, "10:00"),
                                 reason="fever", call_sid="CA123")  # fmt: skip
    assert outcome.ok and "Booked for Asha Rao" in outcome.message
    assert cal.bookings["uid1"]["body"]["attendee"]["phoneNumber"] == "+919876543210"
    assert cal.bookings["uid1"]["body"]["metadata"]["callSid"] == "CA123"
    async with session_factory() as session:
        row = (await session.scalars(select(Appointment))).one()
    assert (row.source, row.external_id, row.status) == ("calcom", "uid1", "booked")
    assert row.booked_via_call == "CA123" and row.phone == "9876543210"
    assert appts.as_aware(row.slot_start) == slot(cal, "10:00").astimezone(UTC)
    assert slot(cal, "10:00") not in await backend.free_slots(day_of(cal))


async def test_a_slot_cal_does_not_offer_is_never_booked(backend: CalBackend, cal: FakeCal):
    outcome = await backend.book(name="Asha Rao", phone="9876543210", slot=slot(cal, "12:00"),
                                 reason="", call_sid="")  # fmt: skip
    assert not outcome.ok
    assert not [c for c in cal.calls if c[0] == "POST"]


async def test_a_slot_lost_in_a_race_is_reported_and_not_mirrored(
    backend: CalBackend, cal: FakeCal, session_factory: Factory
):
    # someone books it in Cal.diy between our slot check and our booking request
    real_slots = backend.client.slots

    async def stale(day):  # our view is out of date: still shows 10:00
        return await real_slots(day)

    cal.taken.add(_utc(cal.slot_starts()[0]))
    backend.client.slots = stale  # type: ignore[method-assign]
    backend.free_slots = lambda d: _async([slot(cal, "10:00")])  # type: ignore[method-assign]
    outcome = await backend.book(name="Asha Rao", phone="9876543210", slot=slot(cal, "10:00"),
                                 reason="", call_sid="")  # fmt: skip
    assert not outcome.ok and "just taken" in outcome.message
    async with session_factory() as session:
        assert not list(await session.scalars(select(Appointment)))


async def _async(value):
    return value


async def test_cal_being_down_raises_so_the_agent_does_not_promise(
    backend: CalBackend, cal: FakeCal
):
    cal.down = True
    with pytest.raises(CalError):
        await backend.book(name="Asha Rao", phone="9876543210", slot=slot(cal, "10:00"),
                           reason="", call_sid="")  # fmt: skip


# ---------- backend: cancel / list ----------


async def test_cancel_cancels_in_cal_and_in_the_mirror(
    backend: CalBackend, cal: FakeCal, session_factory: Factory
):
    await backend.book(name="Asha Rao", phone="9876543210", slot=slot(cal, "17:00"),
                       reason="", call_sid="")  # fmt: skip
    assert len(await backend.upcoming("9876543210")) == 1
    outcome = await backend.cancel("9876543210", None)
    assert outcome.ok and cal.bookings["uid1"]["status"] == "cancelled"
    async with session_factory() as session:
        assert (await session.scalars(select(Appointment.status))).one() == "cancelled"
    assert not (await backend.cancel("9876543210", None)).ok  # nothing left
    assert slot(cal, "17:00") in await backend.free_slots(day_of(cal))  # freed in Cal.diy too


async def test_an_old_local_booking_cancels_without_calling_cal(
    backend: CalBackend, cal: FakeCal, session_factory: Factory
):
    async with session_factory() as session:
        session.add(Appointment(patient_name="Old", phone="9000000001",
                                slot_start=slot(cal, "10:15").astimezone(UTC)))  # fmt: skip
        await session.commit()
    assert (await backend.cancel("9000000001", None)).ok
    assert not [c for c in cal.calls if c[1].endswith("/cancel")]


async def test_a_booking_already_gone_in_cal_is_still_cancelled_here(
    backend: CalBackend, cal: FakeCal, session_factory: Factory
):
    await backend.book(name="Asha Rao", phone="9876543210", slot=slot(cal, "10:00"),
                       reason="", call_sid="")  # fmt: skip
    cal.bookings.clear()  # Cal.diy no longer knows it (404)
    assert (await backend.cancel("9876543210", None)).ok


# ---------- webhooks (bookings made or changed inside Cal.diy) ----------


@pytest_asyncio.fixture
async def web(session_factory: Factory) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_settings] = lambda: settings_for_cal()
    app.state.session_factory = session_factory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


def event(trigger: str, uid: str = "wb1", **payload: object) -> bytes:
    start = (datetime.now(UTC) + timedelta(days=3)).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    data = {"uid": uid, "startTime": start.isoformat(), "status": "ACCEPTED",
            "attendees": [{"name": "Ravi Kumar", "email": "r@example.com",
                           "phoneNumber": "+919123456780"}],
            "additionalNotes": "check-up", **payload}  # fmt: skip
    return json.dumps({"triggerEvent": trigger, "createdAt": "now", "payload": data}).encode()


def signed(body: bytes, secret: str = "whsec") -> dict[str, str]:
    return {"x-cal-signature-256": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "content-type": "application/json"}  # fmt: skip


async def test_webhook_needs_a_valid_signature(web: httpx.AsyncClient):
    body = event("BOOKING_CREATED")
    assert (await web.post("/webhooks/calcom", content=body)).status_code == 403
    assert (await web.post("/webhooks/calcom", content=body,
                           headers=signed(body, "wrong"))).status_code == 403  # fmt: skip
    ok = await web.post("/webhooks/calcom", content=body, headers=signed(body))
    assert ok.status_code == 200 and ok.json() == {"result": "created"}


async def test_webhook_is_off_without_a_secret(web: httpx.AsyncClient):
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    body = event("BOOKING_CREATED")
    assert (
        await web.post("/webhooks/calcom", content=body, headers=signed(body))
    ).status_code == 503


async def test_a_booking_made_in_cal_appears_in_our_database(
    web: httpx.AsyncClient, session_factory: Factory
):
    body = event("BOOKING_CREATED")
    await web.post("/webhooks/calcom", content=body, headers=signed(body))
    await web.post("/webhooks/calcom", content=body, headers=signed(body))  # delivered twice
    async with session_factory() as session:
        row = (await session.scalars(select(Appointment))).one()
    assert (row.patient_name, row.phone, row.reason) == ("Ravi Kumar", "9123456780", "check-up")
    assert (row.source, row.external_id, row.status, row.booked_via_call) == (
        "calcom", "wb1", "booked", None)  # fmt: skip


async def test_cancel_and_reschedule_in_cal_are_followed(
    web: httpx.AsyncClient, session_factory: Factory
):
    for body in (event("BOOKING_CREATED"),
                 event("BOOKING_RESCHEDULED", uid="wb2", rescheduleUid="wb1")):  # fmt: skip
        await web.post("/webhooks/calcom", content=body, headers=signed(body))
    async with session_factory() as session:
        rows = {r.external_id: r.status for r in await session.scalars(select(Appointment))}
    assert rows == {"wb1": "cancelled", "wb2": "booked"}  # the old one is replaced
    body = event("BOOKING_CANCELLED", uid="wb2", status="CANCELLED")
    assert (await web.post("/webhooks/calcom", content=body, headers=signed(body))).json() == {
        "result": "cancelled"}  # fmt: skip
    async with session_factory() as session:
        assert set(await session.scalars(select(Appointment.status))) == {"cancelled"}


async def test_webhook_ignores_junk(web: httpx.AsyncClient):
    for body in (event("PING"), event("BOOKING_CANCELLED", uid="never-seen")):
        resp = await web.post("/webhooks/calcom", content=body, headers=signed(body))
        assert resp.json() == {"result": "ignored"}
    bad = b"not json"
    assert (await web.post("/webhooks/calcom", content=bad, headers=signed(bad))).status_code == 400


# ---------- choosing the backend ----------


def test_the_backend_follows_the_setting(session_factory: Factory):
    assert isinstance(make_backend(Settings(_env_file=None), session_factory), LocalBackend)
    with pytest.raises(ValueError):
        make_backend(Settings(_env_file=None, booking_backend="calcom"), session_factory)
    a = make_backend(settings_for_cal(), session_factory)
    b = make_backend(settings_for_cal(), session_factory)
    assert isinstance(a, CalBackend) and a.client is b.client  # one shared connection pool


# ---------- the voice tools on top of Cal.diy ----------


async def test_the_agent_books_and_cancels_through_cal(
    backend: CalBackend, cal: FakeCal, session_factory: Factory
):
    class Recorder:
        booked = cancelled = False

        async def add_event(self, *a: object, **k: object) -> None:
            return None

    rec = Recorder()
    tools = build_tools(settings_for_cal(), session_factory, caller_number="+919876543210",
                        call_sid="CA9", recorder=rec, backend=backend)  # fmt: skip
    day = cal.day
    slots = await call(tools, "check_available_slots", date=day)
    assert slots["ok"] and [s["time"] for s in slots["free_slots"]][:2] == ["10:00", "10:15"]
    booked = await call(tools, "book_appointment", patient_name="Asha Rao", date=day,
                        time="10:00", reason="fever")  # fmt: skip
    assert booked["ok"] and rec.booked  # the reply guard may now allow "booked"
    assert len((await call(tools, "my_appointments"))["appointments"]) == 1
    assert (await call(tools, "cancel_appointment", date=day))["ok"] and rec.cancelled


async def test_the_agent_does_not_promise_when_cal_is_down(
    backend: CalBackend, cal: FakeCal, session_factory: Factory
):
    cal.down = True
    tools = build_tools(settings_for_cal(), session_factory, caller_number="9876543210",
                        call_sid="CA9", backend=backend)  # fmt: skip
    result = await call(tools, "check_available_slots", date=cal.day)
    assert not result["ok"] and "do not promise" in result["message"]


def test_fake_params_are_shared_with_the_other_tests():
    assert FakeParams().result is None  # keeps the import honest


# ---------- dashboard: the Calendar section ----------


async def calendar_json(web: httpx.AsyncClient) -> dict:
    await web.post("/dashboard/api/login", json={"password": "dash"})
    return (await web.get("/dashboard/api/calendar")).json()


@pytest_asyncio.fixture
async def dash(session_factory: Factory) -> AsyncIterator[httpx.AsyncClient]:
    from app.business import dashboard_stats

    dashboard_stats._calendar_cache.clear()
    app.state.session_factory = session_factory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()
    dashboard_stats._calendar_cache.clear()


async def test_the_calendar_needs_a_login(dash: httpx.AsyncClient):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, dashboard_password=SecretStr("dash")
    )
    assert (await dash.get("/dashboard/api/calendar")).status_code == 401


async def test_the_built_in_calendar_shows_the_week(dash: httpx.AsyncClient):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, dashboard_password=SecretStr("dash"), cal_public_url=""
    )
    cal = await calendar_json(dash)
    assert cal["backend"] == "local" and cal["status"] == "ok" and cal["links"] == []
    assert len(cal["days"]) == 7
    closed = [d for d in cal["days"] if d["closed"]]
    assert all(d["free"] == 0 for d in closed)  # Sundays are closed by default
    assert any(d["free"] > 0 and d["times"] for d in cal["days"] if not d["closed"])


async def test_the_calendar_reads_cal_diy_and_links_into_it(
    dash: httpx.AsyncClient, cal: FakeCal, backend: CalBackend, monkeypatch: pytest.MonkeyPatch
):
    from app.business import dashboard_stats

    monkeypatch.setattr(dashboard_stats, "make_backend", lambda s, f: backend)
    app.dependency_overrides[get_settings] = lambda: settings_for_cal(
        dashboard_password=SecretStr("dash"),
        cal_public_url="https://cal.example.com/",
        cal_booking_page="dr-sharma/consultation",
    )
    data = await calendar_json(dash)
    assert data["backend"] == "calcom" and data["status"] == "ok"
    day = next(d for d in data["days"] if d["date"] == cal.day)
    assert day["free"] == 3 and day["times"] == ["10:00", "10:15", "17:00"]
    assert {link["label"]: link["url"] for link in data["links"]} == {
        "Bookings": "https://cal.example.com/bookings/upcoming",
        "Availability": "https://cal.example.com/availability",
        "Event types": "https://cal.example.com/event-types",
        "Patient booking page": "https://cal.example.com/dr-sharma/consultation",
    }
    calls_before = len(cal.calls)
    await dash.get("/dashboard/api/calendar")  # a refresh within 45 s does not hit Cal.diy again
    assert len(cal.calls) == calls_before


async def test_the_calendar_says_so_when_cal_diy_is_down_or_not_set_up(
    dash: httpx.AsyncClient, cal: FakeCal, backend: CalBackend, monkeypatch: pytest.MonkeyPatch
):
    from app.business import dashboard_stats

    cal.down = True
    monkeypatch.setattr(dashboard_stats, "make_backend", lambda s, f: backend)
    app.dependency_overrides[get_settings] = lambda: settings_for_cal(
        dashboard_password=SecretStr("dash")
    )
    down = await calendar_json(dash)
    assert down["status"] == "error" and "Cal.diy" in down["error"]

    monkeypatch.undo()
    dashboard_stats._calendar_cache.clear()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, dashboard_password=SecretStr("dash"), booking_backend="calcom"
    )
    missing = (await dash.get("/dashboard/api/calendar")).json()
    assert missing["status"] == "not_configured" and "CAL_API_KEY" in missing["error"]
