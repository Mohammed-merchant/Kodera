"""Client for the Smart Hospital Management System REST API (BOOKING_BACKEND=hms).

The voice agent signs in with its own ADMIN account (HMS_EMAIL / HMS_PASSWORD) and uses:

    GET  /users/doctors                                       doctors (public), cached
    GET  /appointments/available-slots?doctorId=&date=        free slots of one doctor, one day
    GET  /users?role=PATIENT&page=&limit=                     patients (to find the caller)
    POST /auth/register {name, email, password, role, phone}  a new patient
    POST /appointments {patient, doctor, department, date, time, reason, notes}  book
    PUT  /appointments/{id}  {"status": "CANCELLED"}          cancel

Checked against the live server: its routes and field names differ from the API document.

Times in the HMS are clinic-local labels like "05:00 PM"; this client turns them into
datetime.time and back. Every response may come wrapped as {"success", "data"} or bare.
"""

import difflib
import re
import secrets
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

DOCTORS_TTL_SECONDS = 600


class HmsError(Exception):
    """The HMS refused (4xx: `rejected` is True) or could not be reached / failed."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def rejected(self) -> bool:
        return self.status is not None and 400 <= self.status < 500


@dataclass(frozen=True)
class Doctor:
    id: str
    name: str
    specialization: str
    department_id: str
    department: str
    fee: float | None

    def describe(self) -> str:
        fee = f", fee {self.fee:g}" if self.fee is not None else ""
        area = self.specialization or self.department
        return f"{self.name}" + (f" ({area}{fee})" if area or fee else "")


def slot_label(t: time) -> str:
    """09:00 -> "09:00 AM", 17:15 -> "05:15 PM" (the HMS format)."""
    return f"{t:%I:%M %p}"


def parse_label(label: str) -> time | None:
    """ "05:15 PM" / "5:15 pm" / "17:15" -> time, or None."""
    text = label.strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _last10(phone: str) -> str:
    return re.sub(r"\D", "", phone)[-10:]


def _unwrap(body: Any) -> Any:
    """{"success": true, "data": X} -> X; anything else unchanged."""
    if isinstance(body, dict) and "data" in body and body.get("success") is not False:
        return body["data"]
    return body


def _doctor(raw: dict[str, Any]) -> Doctor:
    dept = raw.get("department")
    fee = raw.get("consultationFee")
    return Doctor(
        id=str(raw.get("_id", "")),
        name=str(raw.get("name", "")).strip(),
        specialization=str(raw.get("specialization") or "").strip(),
        department_id=str(dept.get("_id", "")) if isinstance(dept, dict) else str(dept or ""),
        department=str(dept.get("name", "")) if isinstance(dept, dict) else "",
        fee=float(fee) if isinstance(fee, int | float) else None,
    )


class HmsClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,  # tests plug in a fake HMS here
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email, self.password = email, password
        self._token: str | None = None
        self._doctors: tuple[float, list[Doctor]] | None = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=300),
            transport=transport,
        )

    async def _login(self) -> str:
        resp = await self._http.post(
            f"{self.base_url}/auth/login", json={"email": self.email, "password": self.password}
        )
        body = resp.json() if resp.content else {}
        inner = _unwrap(body)
        token = None
        for part in (body, inner):  # {"token": ...} or {"data": {"token": ...}}
            if isinstance(part, dict) and part.get("token"):
                token = part["token"]
                break
        if resp.status_code >= 400 or not token:
            raise HmsError(f"login failed ({resp.status_code})", status=resp.status_code)
        self._token = str(token)
        return self._token

    async def request(
        self, method: str, path: str, *, json: Any = None, params: Any = None, auth: bool = True
    ) -> Any:
        """One API call. A 401 means the token expired: sign in again and retry once."""
        for attempt in (1, 2):
            headers = {}
            if auth:
                headers["Authorization"] = f"Bearer {self._token or await self._login()}"
            try:
                resp = await self._http.request(
                    method, self.base_url + path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                raise HmsError(f"HMS not reachable: {type(exc).__name__}") from exc
            if resp.status_code == 401 and auth and attempt == 1:
                self._token = None
                continue
            body = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                message = body.get("message") if isinstance(body, dict) else None
                raise HmsError(str(message or f"HTTP {resp.status_code}"), status=resp.status_code)
            return _unwrap(body)
        raise HmsError("HMS refused the login token", status=401)

    async def doctors(self, refresh: bool = False) -> list[Doctor]:
        """All doctors, cached for a few minutes (the list rarely changes)."""
        fresh = self._doctors and clock.monotonic() - self._doctors[0] < DOCTORS_TTL_SECONDS
        if fresh and not refresh and self._doctors:
            return self._doctors[1]
        data = await self.request("GET", "/users/doctors", auth=False)
        doctors = (
            [_doctor(d) for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        )
        doctors = [d for d in doctors if d.id and d.name]
        self._doctors = (clock.monotonic(), doctors)
        return doctors

    async def free_slots(self, doctor_id: str, day: date) -> list[time]:
        data = await self.request(
            "GET",
            "/appointments/available-slots",
            params={"doctorId": doctor_id, "date": day.isoformat()},
        )
        labels = data.get("availableSlots", []) if isinstance(data, dict) else []
        return sorted(t for t in (parse_label(str(x)) for x in labels) if t is not None)

    def _alias(self, tag: str) -> str:
        """agent@gmail.com -> agent+<tag>@gmail.com (Gmail delivers it to the agent's inbox)."""
        local, _, domain = self.email.partition("@")
        return f"{local.split('+')[0]}+{tag}@{domain or 'gmail.com'}"

    async def patients(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 51):  # 100 per page; stops after the last page
            rows = await self.request(
                "GET", "/users", params={"role": "PATIENT", "page": page, "limit": 100}
            )
            rows = rows if isinstance(rows, list) else []
            out += [r for r in rows if isinstance(r, dict)]
            if len(rows) < 100:
                break
        return out

    async def patient_id(self, name: str, phone_e164: str) -> str:
        """The caller's patient record: same phone AND a similar name (a family shares one phone;
        each person has their own record). Created when there is none yet."""
        digits = _last10(phone_e164)
        wanted = name.strip().lower()
        for p in await self.patients():
            if _last10(str(p.get("phone", ""))) != digits:
                continue
            known = str(p.get("name", "")).strip().lower()
            close = difflib.SequenceMatcher(None, wanted, known).ratio() >= 0.75
            if close or wanted.split()[:1] == known.split()[:1]:
                return str(p["_id"])
        # Callers have no e-mail. The HMS only accepts @gmail.com, so the patient gets a "+" alias
        # of the agent's OWN Gmail (ai.agent+p91...@gmail.com): any mail lands in the clinic's own
        # inbox, never at a stranger's address. The random password is never needed.
        body = await self.request("POST", "/auth/register", auth=False, json={
            "name": name.strip()[:100],
            "email": self._alias(f"p{digits}.{secrets.token_hex(2)}"),
            "password": secrets.token_urlsafe(18),
            "role": "PATIENT",
            "phone": phone_e164,
        })  # fmt: skip
        user: Any = None
        for part in (body, body.get("user") if isinstance(body, dict) else None):
            if isinstance(part, dict):
                user = part.get("user") if isinstance(part.get("user"), dict) else part
                if isinstance(user, dict) and user.get("_id"):
                    break
        if not isinstance(user, dict) or not user.get("_id"):
            raise HmsError("the HMS did not return the new patient")
        log.info("hms_patient_created", patient=str(user["_id"]))
        return str(user["_id"])

    async def book(
        self, doctor: Doctor, day: date, slot: time, *, reason: str, patient_id: str, note: str = ""
    ) -> str:
        """Book and return the HMS appointment id."""
        body: dict[str, Any] = {
            "patient": patient_id,
            "doctor": doctor.id,
            "department": doctor.department_id or None,
            "date": day.isoformat(),
            "time": slot_label(slot),
            "reason": (reason.strip() or "Consultation")[:300],
            "notes": note[:300],
        }
        data = await self.request(
            "POST", "/appointments", json={k: v for k, v in body.items() if v is not None}
        )
        appt = data.get("appointment", data) if isinstance(data, dict) else {}
        appt_id = appt.get("_id") or appt.get("id") if isinstance(appt, dict) else None
        if not appt_id:
            raise HmsError("the HMS did not return an appointment id")
        return str(appt_id)

    async def cancel(self, appointment_id: str) -> None:
        await self.request("PUT", f"/appointments/{appointment_id}", json={"status": "CANCELLED"})
