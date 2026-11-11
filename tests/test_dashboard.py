from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business import appointments as appts
from app.core import dashboard_auth
from app.core.config import Settings, get_settings
from app.db.models import Appointment, Call
from app.main import app

PASSWORD = "correct horse"


class FakeRedis:
    async def ping(self) -> bool:
        return True

    async def info(self, section: str) -> dict[str, Any]:
        return {"used_memory_human": "1.00M", "redis_version": "7.4.0"}

    async def dbsize(self) -> int:
        return 3


def settings_with(password: str = PASSWORD) -> Settings:
    return Settings(_env_file=None, dashboard_password=SecretStr(password))


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    dashboard_auth.limiter._failures.clear()
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.dependency_overrides[get_settings] = lambda: settings_with()
    app.state.session_factory = session_factory
    app.state.redis = FakeRedis()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/dashboard/api/login", json={"password": password})


# ---------- switched off / locked ----------


async def test_dashboard_is_off_without_a_password(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: settings_with("")
    assert (await client.get("/dashboard")).status_code == 404
    assert (await login(client)).status_code == 404
    assert (await client.get("/dashboard/api/overview")).status_code == 404


async def test_data_needs_a_login(client: httpx.AsyncClient) -> None:
    assert (await client.get("/dashboard/api/overview")).status_code == 401


async def test_page_is_public_but_carries_no_data_and_is_locked_down(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/dashboard")
    assert r.status_code == 200 and "<title>Clinic Dashboard</title>" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "noindex" in r.headers["x-robots-tag"]
    assert "default-src 'none'" in r.headers["content-security-policy"]


async def test_wrong_password_then_rate_limit(client: httpx.AsyncClient) -> None:
    for _ in range(dashboard_auth.MAX_FAILURES):
        assert (await login(client, "nope")).status_code == 401
    assert (await login(client, "nope")).status_code == 429
    assert (await login(client)).status_code == 429  # even the right password, until it expires


async def test_login_sets_a_safe_cookie_and_logout_clears_it(client: httpx.AsyncClient) -> None:
    r = await login(client)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/dashboard" in cookie
    assert (await client.get("/dashboard/api/overview")).status_code == 200
    await client.post("/dashboard/api/logout")
    assert (await client.get("/dashboard/api/overview")).status_code == 401


def test_tokens_expire_and_cannot_be_forged() -> None:
    s = settings_with()
    now = 1_000_000.0
    token = dashboard_auth.make_token(s, now)
    assert dashboard_auth.token_is_valid(s, token, now + 60)
    assert not dashboard_auth.token_is_valid(s, token, now + 13 * 3600)  # past 12 hours
    expires, _, _ = token.partition(".")
    assert not dashboard_auth.token_is_valid(s, f"{expires}.{'0' * 64}", now)
    assert not dashboard_auth.token_is_valid(s, "garbage", now)
    assert not dashboard_auth.token_is_valid(settings_with("other password"), token, now)


# ---------- the numbers ----------


async def test_overview_shows_bookings_calls_and_performance(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    sched = appts.schedule_from_settings(settings_with())
    soon = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    async with session_factory() as session:
        session.add_all(
            [
                Call(
                    provider_call_id="CA_BOOKED",
                    from_number="+919876543210",
                    status="completed",
                    duration_seconds=90,
                ),
                Call(
                    provider_call_id="CA_PLAIN",
                    from_number="+919123456780",
                    status="ringing",
                    started_at=datetime.now(UTC) - timedelta(hours=2),
                ),
                Appointment(
                    patient_name="Asha Rao",
                    phone="9876543210",
                    reason="fever",
                    slot_start=soon,
                    booked_via_call="CA_BOOKED",
                ),
                Appointment(
                    patient_name="Old Visit",
                    phone="9000000001",
                    slot_start=soon - timedelta(days=9),
                ),
                Appointment(
                    patient_name="Gone",
                    phone="9000000002",
                    status="cancelled",
                    slot_start=soon + timedelta(days=1),
                ),
            ]
        )
        await session.commit()

    await login(client)
    body = (await client.get("/dashboard/api/overview")).json()

    assert body["clinic"] == "City Care Clinic"
    kpis = body["kpis"]
    assert kpis["appointments_upcoming"] == 1 and kpis["calls_total"] == 2
    assert kpis["avg_call_seconds"] == 90
    assert kpis["bookings_7d"] == 1 and kpis["booking_rate_percent"] == 50

    upcoming = body["appointments"]["upcoming"]
    assert [a["patient"] for a in upcoming] == ["Asha Rao"] and upcoming[0]["by_phone_agent"]
    assert [a["patient"] for a in body["appointments"]["past"]] == ["Old Visit"]
    assert [a["patient"] for a in body["appointments"]["cancelled"]] == ["Gone"]
    assert "AM" in upcoming[0]["time"] or "PM" in upcoming[0]["time"]

    calls = {c["from"]: c for c in body["calls"]}
    assert set(calls) == {"***3210", "***6780"}  # numbers are masked
    assert calls["***3210"]["booked"] is True and calls["***6780"]["booked"] is False
    assert calls["***6780"]["status"] == "no status"  # stuck on "ringing" for 2 hours

    perf = body["performance"]
    assert perf["postgres_ping_ms"] >= 0 and perf["slot_lookup_ms"] >= 0
    assert "list upcoming appointments" in perf["query_ms"]
    assert perf["redis"] == {
        "ping_ms": perf["redis"]["ping_ms"],
        "version": "7.4.0",
        "memory": "1.00M",
        "keys": 3,
    }
    assert perf["postgres"] is None  # SQLite in tests has no Postgres statistics
    assert sched.tz is not None


async def test_overview_survives_an_empty_database_and_a_dead_redis(
    client: httpx.AsyncClient,
) -> None:
    class DeadRedis(FakeRedis):
        async def ping(self) -> bool:
            raise ConnectionError("down")

    app.state.redis = DeadRedis()
    await login(client)
    body = (await client.get("/dashboard/api/overview")).json()
    assert body["kpis"]["calls_total"] == 0 and body["kpis"]["booking_rate_percent"] is None
    assert body["calls"] == [] and body["performance"]["redis"] is None
