from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import Call, CallEvent
from app.events.publisher import STREAM
from app.main import app
from tests.fakes import FakeRedis

SECRET = "hook-secret"
ACCOUNT = "exotel_acct"
SID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
PASSTHRU = f"/webhooks/exotel/passthru?token={SECRET}"
STATUS = f"/webhooks/exotel/status?token={SECRET}"
JSON_BODY = {
    "CallSid": SID,
    "StreamSid": "MZ1",
    "StreamUrl": "wss://example.com/ws/exotel",
    "AccountSid": ACCOUNT,
    "EventType": "stream-start",
    "From": "+919876543210",
    "To": "+918041234567",
    "Direction": "inbound",
}
FORM_BODY = {
    "CallSid": SID,
    "CallFrom": "+919876543210",
    "CallTo": "+918041234567",
    "Direction": "incoming",
}


class Env:
    def __init__(
        self, client: httpx.AsyncClient, redis: FakeRedis, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        self.client = client
        self.redis = redis
        self.sf = sf


def _settings(**extra: Any) -> Settings:
    values: dict[str, Any] = {
        "call_provider": "exotel",
        "exotel_webhook_secret": SecretStr(SECRET),
        "exotel_account_sid": ACCOUNT,
        **extra,
    }
    return Settings(_env_file=None, **values)


@pytest_asyncio.fixture
async def env(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Env]:
    redis = FakeRedis()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Env(client, redis, session_factory)
    app.dependency_overrides.clear()


# --- passthru -------------------------------------------------------------------------


async def test_agentstream_json_passthru_saves_call_and_returns_200(env: Env) -> None:
    r = await env.client.post(PASSTHRU, json=JSON_BODY)
    assert r.status_code == 200
    assert r.content == b""  # empty body: the flow continues
    async with env.sf() as s:
        call = (await s.execute(select(Call))).scalar_one()
        assert call.provider == "exotel"
        assert call.provider_call_id == SID
        assert call.direction == "inbound"
        assert call.from_number == "+919876543210"
        events = (await s.execute(select(CallEvent))).scalars().all()
        assert [e.type for e in events] == ["call.started"]
    assert [f["type"] for _id, f in env.redis.streams[STREAM]] == ["call.started"]


async def test_classic_form_passthru_works(env: Env) -> None:
    r = await env.client.post(PASSTHRU, data=FORM_BODY)
    assert r.status_code == 200
    async with env.sf() as s:
        call = (await s.execute(select(Call))).scalar_one()
        assert call.to_number == "+918041234567"
        assert call.direction == "inbound"  # "incoming" is normalised


async def test_classic_get_passthru_works(env: Env) -> None:
    r = await env.client.get("/webhooks/exotel/passthru", params={**FORM_BODY, "token": SECRET})
    assert r.status_code == 200
    async with env.sf() as s:
        assert (await s.execute(select(Call))).scalar_one().provider_call_id == SID


async def test_repeated_passthru_is_saved_once_but_always_answered(env: Env) -> None:
    for _ in range(3):
        assert (await env.client.post(PASSTHRU, json=JSON_BODY)).status_code == 200
    async with env.sf() as s:
        assert len((await s.execute(select(CallEvent))).scalars().all()) == 1


async def test_database_failure_still_returns_200(env: Env) -> None:
    def broken() -> Any:
        raise RuntimeError("db down")

    app.state.session_factory = broken
    assert (await env.client.post(PASSTHRU, json=JSON_BODY)).status_code == 200


async def test_secret_token_is_not_stored_or_returned(env: Env) -> None:
    r = await env.client.post(PASSTHRU, json=JSON_BODY)
    assert SECRET not in r.text
    async with env.sf() as s:
        for e in (await s.execute(select(CallEvent))).scalars().all():
            assert SECRET not in str(e.data)


# --- security -------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?token=", "?token=wrong", f"?token={SECRET}x", "?x=1"])
async def test_wrong_or_missing_token_is_rejected(env: Env, query: str) -> None:
    r = await env.client.post(f"/webhooks/exotel/passthru{query}", json=JSON_BODY)
    assert r.status_code == 403
    async with env.sf() as s:
        assert (await s.execute(select(Call))).first() is None


async def test_status_endpoint_also_requires_token(env: Env) -> None:
    r = await env.client.post(
        "/webhooks/exotel/status", data={"CallSid": SID, "Status": "completed"}
    )
    assert r.status_code == 403


async def test_other_exotel_account_is_rejected(env: Env) -> None:
    r = await env.client.post(PASSTHRU, json={**JSON_BODY, "AccountSid": "someone_else"})
    assert r.status_code == 403


async def test_unconfigured_secret_returns_503(env: Env) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, call_provider="exotel", exotel_webhook_secret=SecretStr("")
    )
    r = await env.client.post("/webhooks/exotel/passthru?token=", json=JSON_BODY)
    assert r.status_code == 503


async def test_ip_allowlist_blocks_other_addresses(env: Env) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(exotel_allowed_ips="198.51.100.1")
    assert (await env.client.post(PASSTHRU, json=JSON_BODY)).status_code == 403


async def test_ip_allowlist_allows_listed_address(env: Env) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(
        exotel_allowed_ips="198.51.100.1, 203.0.113.9"
    )
    assert (await env.client.post(PASSTHRU, json=JSON_BODY)).status_code == 200


async def test_routes_answer_404_when_twilio_is_the_active_provider(env: Env) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(call_provider="twilio")
    assert (await env.client.post(PASSTHRU, json=JSON_BODY)).status_code == 404
    assert (await env.client.post(STATUS, data={"CallSid": SID})).status_code == 404


async def test_oversized_body_is_rejected(env: Env) -> None:
    r = await env.client.post(
        PASSTHRU, content=b"x" * 70_000, headers={"content-type": "text/plain"}
    )
    assert r.status_code == 413


async def test_invalid_json_is_rejected(env: Env) -> None:
    r = await env.client.post(
        PASSTHRU, content=b"{not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    r = await env.client.post(
        PASSTHRU, content=b"[1, 2]", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400


@pytest.mark.parametrize("sid", ["", "bad sid", "a;b;c;d;e;f"])
async def test_bad_call_sid_is_rejected(env: Env, sid: str) -> None:
    r = await env.client.post(PASSTHRU, json={**JSON_BODY, "CallSid": sid})
    assert r.status_code == 400


async def test_webhooks_hidden_from_openapi(env: Env) -> None:
    paths = (await env.client.get("/openapi.json")).json()["paths"]
    assert not [p for p in paths if p.startswith("/webhooks")]


# --- status ---------------------------------------------------------------------------


async def test_status_completed_updates_call_and_publishes(env: Env) -> None:
    await env.client.post(PASSTHRU, json=JSON_BODY)
    r = await env.client.post(
        STATUS, data={"CallSid": SID, "Status": "completed", "Duration": "12"}
    )
    assert r.status_code == 200
    async with env.sf() as s:
        call = (await s.execute(select(Call))).scalar_one()
        assert call.status == "completed"
        assert call.duration_seconds == 12
        assert call.ended_at is not None
    assert [f["type"] for _id, f in env.redis.streams[STREAM]] == ["call.started", "call.completed"]


async def test_status_accepts_other_likely_field_names(env: Env) -> None:
    r = await env.client.post(
        STATUS, data={"CallSid": SID, "CallStatus": "no_answer", "DialCallDuration": "0"}
    )
    assert r.status_code == 200
    async with env.sf() as s:
        assert (await s.execute(select(Call))).scalar_one().status == "no-answer"


async def test_repeated_status_is_saved_once(env: Env) -> None:
    form = {"CallSid": SID, "Status": "completed"}
    await env.client.post(STATUS, data=form)
    assert (await env.client.post(STATUS, data=form)).status_code == 200
    assert len(env.redis.streams[STREAM]) == 1


@pytest.mark.parametrize(
    "form",
    [
        {"CallSid": SID},  # no status
        {"CallSid": SID, "Status": "completed; DROP"},
        {"CallSid": SID, "Status": "completed", "Duration": "1e9"},
        {"Status": "completed"},  # no CallSid
    ],
)
async def test_status_with_bad_values_is_rejected(env: Env, form: dict[str, str]) -> None:
    assert (await env.client.post(STATUS, data=form)).status_code == 400


async def test_status_save_failure_returns_500_and_a_retry_succeeds(env: Env) -> None:
    good = app.state.session_factory

    def broken() -> Any:
        raise RuntimeError("db down")

    app.state.session_factory = broken
    form = {"CallSid": SID, "Status": "completed"}
    with pytest.raises(RuntimeError):
        await env.client.post(STATUS, data=form)
    app.state.session_factory = good
    assert (await env.client.post(STATUS, data=form)).status_code == 200
