"""Smart Hospital Management System backend, against a fake HMS that follows its API doc."""

import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.business.booking_backend import DoctorNeeded, HmsBackend, pick_doctor
from app.core.config import Settings
from app.integrations.hms import Doctor, HmsClient, parse_label, slot_label
from app.pipeline.tools import build_tools

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings(_env_file=None)
SCHED = appts.schedule_from_settings(SETTINGS)
DOCTORS = [
    {"_id": "d1", "name": "Dr. Robert Chen", "specialization": "General Medicine",
     "department": {"_id": "g1", "name": "General Medicine"}, "consultationFee": 80},
    {"_id": "d2", "name": "Dr. Sarah Jenkins", "specialization": "Cardiology",
     "department": {"_id": "c1", "name": "Cardiology"}, "consultationFee": 120},
]  # fmt: skip


class FakeHms:
    """Login, doctors, patients, free slots, booking and cancelling, as the LIVE server does
    (its routes and field names differ from the API document)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.taken: set[tuple[str, str, str]] = set()  # (doctor, date, "05:00 PM")
        self.cancelled: list[str] = []
        self.patients: list[dict[str, Any]] = []
        self.expire_token_once = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api")
        self.calls.append((request.method, path, body))
        if path == "/auth/login":
            ok = body == {"email": "ai@clinic", "password": "pw"}
            return httpx.Response(
                200 if ok else 401, json={"success": ok, "token": "T" if ok else None}
            )
        if path == "/auth/register":
            if not (body and body.get("name") and body.get("email") and body.get("password")):
                return httpx.Response(
                    400,
                    json={"success": False, "message": "Please provide name, email, and password"},
                )
            user = {
                "_id": f"p{len(self.patients) + 1}",
                "name": body["name"],
                "phone": body.get("phone"),
                "role": "PATIENT",
            }
            self.patients.append(user)
            return httpx.Response(201, json={"success": True, "token": "X", "user": user})
        if path == "/users/doctors":
            return httpx.Response(200, json={"success": True, "data": DOCTORS})
        if request.headers.get("Authorization") != "Bearer T" or self.expire_token_once:
            self.expire_token_once = False
            return httpx.Response(401, json={"success": False, "message": "Not authorized"})
        if path == "/users" and request.method == "GET":
            return httpx.Response(
                200, json={"success": True, "data": self.patients, "pagination": {}}
            )
        if path == "/appointments/available-slots":
            doctor, day = request.url.params["doctorId"], request.url.params["date"]
            free = [
                s
                for s in ("10:00 AM", "05:00 PM", "05:15 PM")
                if (doctor, day, s) not in self.taken
            ]
            return httpx.Response(200, json={"success": True, "data": {"availableSlots": free}})
        if path == "/appointments" and request.method == "POST":
            if not body.get("patient"):
                return httpx.Response(
                    400, json={"success": False, "message": "Patient ID is required"}
                )
            if not all(body.get(k) for k in ("doctor", "date", "time", "reason")):
                return httpx.Response(
                    400,
                    json={
                        "success": False,
                        "message": "Doctor, date, time, and reason are required",
                    },
                )
            key = (body["doctor"], body["date"], body["time"])
            if key in self.taken:
                return httpx.Response(
                    409, json={"success": False, "message": "Slot already booked"}
                )
            self.taken.add(key)
            return httpx.Response(
                201, json={"success": True, "data": {"_id": f"a{len(self.taken)}"}}
            )
        if (
            path.startswith("/appointments/")
            and request.method == "PUT"
            and body == {"status": "CANCELLED"}
        ):
            self.cancelled.append(path.split("/")[2])
            return httpx.Response(200, json={"success": True, "data": {}})
        return httpx.Response(404, json={"success": False, "message": "Not Found - " + path})


@pytest.fixture
def hms() -> FakeHms:
    return FakeHms()


def backend(hms: FakeHms, factory: Factory, default: str = "") -> HmsBackend:
    client = HmsClient(
        "https://hms.test/api", "ai@clinic", "pw", transport=httpx.MockTransport(hms.handler)
    )
    return HmsBackend(client, factory, SCHED, default)


def tomorrow() -> date:
    return SCHED.now().date() + timedelta(days=1)


def test_time_labels_round_trip() -> None:
    for label in ("10:00 AM", "05:15 PM", "12:00 PM", "12:30 AM"):
        t = parse_label(label)
        assert t is not None and slot_label(t) == label
    assert parse_label("5:15 pm") == parse_label("17:15") and parse_label("soon") is None


def test_the_right_doctor_is_picked() -> None:
    docs = [Doctor("d1", "Dr. Robert Chen", "General Medicine", "g1", "General Medicine", 80),
            Doctor("d2", "Dr. Sarah Jenkins", "Cardiology", "c1", "Cardiology", 120)]  # fmt: skip
    assert pick_doctor(docs, "Dr. Chen").id == "d1"
    assert pick_doctor(docs, "jenkins").id == "d2"
    assert pick_doctor(docs, "heart doctor").id == "d2"
    assert pick_doctor(docs, "cardiology").id == "d2"
    assert pick_doctor(docs, "", default="Dr. Robert Chen").id == "d1"
    with pytest.raises(DoctorNeeded, match="Sarah Jenkins"):
        pick_doctor(docs, "")  # several doctors, none named: the agent must ask


async def test_slots_booking_copy_and_cancel(hms: FakeHms, session_factory: Factory) -> None:
    be = backend(hms, session_factory)
    free = await be.free_slots(tomorrow(), lead_minutes=0, doctor="heart")
    assert [f"{s:%H:%M}" for s in free] == ["10:00", "17:00", "17:15"]
    slot = datetime.combine(tomorrow(), parse_label("05:00 PM"), tzinfo=SCHED.tz)  # type: ignore[arg-type]
    out = await be.book(name="Asha Rao", phone="9876543210", slot=slot, reason="chest pain check",
                        call_sid="CA1", doctor="Dr. Jenkins")  # fmt: skip
    assert out.ok and "Sarah Jenkins" in out.message
    post = next(c for c in hms.calls if c[0] == "POST" and c[1] == "/appointments")[2]
    assert (post["doctor"], post["department"], post["time"]) == ("d2", "c1", "05:00 PM")
    assert post["reason"] == "chest pain check" and post["patient"] == "p1"
    assert hms.patients[0]["phone"] == "+919876543210" and hms.patients[0]["name"] == "Asha Rao"
    # The copy in our table (dashboard, my_appointments, cancel by phone):
    (row,) = await appts.upcoming(session_factory, SCHED, "9876543210")
    assert (row.source, row.doctor, row.external_id) == ("hms", "Dr. Sarah Jenkins", "hms:a1")
    # Taken now: refused, nothing copied twice.
    again = await be.book(
        name="Ravi", phone="9000000000", slot=slot, reason="x", call_sid="CA2", doctor="heart"
    )
    assert not again.ok and "not available" in again.message
    # Another doctor at the same time is fine (one booking per doctor per slot).
    other = await be.book(
        name="Ravi Kumar",
        phone="9000000000",
        slot=slot,
        reason="fever",
        call_sid="CA2",
        doctor="Dr. Chen",
    )
    assert other.ok
    cancelled = await be.cancel("9876543210", None)
    assert cancelled.ok and hms.cancelled == ["a1"]
    assert await appts.upcoming(session_factory, SCHED, "9876543210") == []


async def test_an_expired_login_is_renewed_once(hms: FakeHms, session_factory: Factory) -> None:
    be = backend(hms, session_factory)
    await be.free_slots(tomorrow(), lead_minutes=0, doctor="Dr. Chen")
    hms.expire_token_once = True
    assert await be.free_slots(tomorrow(), lead_minutes=0, doctor="Dr. Chen")
    assert [c[1] for c in hms.calls].count("/auth/login") == 2


async def test_the_tools_ask_which_doctor(hms: FakeHms, session_factory: Factory) -> None:
    tools = build_tools(SETTINGS, session_factory, caller_number="+919876543210", call_sid="CA9",
                        backend=backend(hms, session_factory))  # fmt: skip
    check = next(t for t in tools if t.info.name == "check_available_slots")
    asked = json.loads(await check(date=tomorrow().isoformat()))
    assert not asked["ok"] and "which doctor" in asked["message"]
    found = json.loads(await check(date=tomorrow().isoformat(), doctor="Dr. Chen"))
    assert found["ok"] and found["free_times"] == ["10:00", "17:00", "17:15"]


async def test_a_family_sharing_one_phone_gets_one_record_each(hms: FakeHms) -> None:
    client = HmsClient(
        "https://hms.test/api", "ai@clinic", "pw", transport=httpx.MockTransport(hms.handler)
    )
    mohammed = await client.patient_id("Mohammed Merchant", "+919876543210")
    rabab = await client.patient_id("Rabab Merchant", "+919876543210")
    again = await client.patient_id("mohammed merchant", "+91 98765 43210")
    assert mohammed != rabab and again == mohammed and len(hms.patients) == 2
    # A "+" alias of the agent's own address (the HMS accepts only @gmail.com addresses).
    emails = [c[2]["email"] for c in hms.calls if c[1] == "/auth/register"]
    assert all(e.startswith("ai+p9876543210.") and e.endswith("@clinic") for e in emails)
