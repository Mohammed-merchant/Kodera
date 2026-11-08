from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from twilio.request_validator import RequestValidator

from app.api.routes.webhooks import GREETING
from app.core.config import Settings, get_settings
from app.core.privacy import mask_number
from app.core.security import verify_stream_token
from app.db.models import Call, CallEvent
from app.events.publisher import STREAM
from app.main import app
from tests.fakes import FakeRedis

TOKEN = "test-auth-token"
BASE = "https://calls.example.com"
ACCOUNT = "AC" + "a" * 32
SID = "CA" + "b" * 32
FORM = {
    "AccountSid": ACCOUNT,
    "CallSid": SID,
    "From": "+15551230000",
    "To": "+15550000000",
    "Direction": "inbound",
}


class Env:
    def __init__(
        self, client: httpx.AsyncClient, redis: FakeRedis, sf: async_sessionmaker[AsyncSession]
    ):
        self.client = client
        self.redis = redis
        self.sf = sf

    async def post(self, path: str, form: dict[str, str]) -> httpx.Response:
        sig = RequestValidator(TOKEN).compute_signature(f"{BASE}{path}", form)
        return await self.client.post(path, data=form, headers={"X-Twilio-Signature": sig})


def _settings(**extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        public_base_url=BASE,
        twilio_auth_token=SecretStr(TOKEN),
        twilio_account_sid=ACCOUNT,
        call_provider="twilio",
        **extra,
    )


@pytest_asyncio.fixture
async def env(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Env]:
    redis = FakeRedis()
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.dependency_overrides[get_settings] = lambda: _settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Env(client, redis, session_factory)
    app.dependency_overrides.clear()


async def test_valid_call_gets_greeting_twiml(env: Env) -> None:
    r = await env.post("/webhooks/twilio/voice", FORM)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<Say>" in r.text and GREETING in r.text
    assert "<Stream" not in r.text  # streaming is off by default


async def test_call_is_saved_and_started_event_published(env: Env) -> None:
    await env.post("/webhooks/twilio/voice", FORM)
    async with env.sf() as s:
        call = (await s.execute(select(Call))).scalar_one()
        assert call.provider_call_id == SID
        assert call.from_number == "+15551230000"
        assert call.status == "ringing"
        events = (await s.execute(select(CallEvent))).scalars().all()
        assert [e.type for e in events] == ["call.started"]
    assert [f["type"] for _id, f in env.redis.streams[STREAM]] == ["call.started"]


async def test_repeated_voice_webhook_is_saved_once(env: Env) -> None:
    for _ in range(3):
        r = await env.post("/webhooks/twilio/voice", FORM)
        assert r.status_code == 200  # the caller is still answered every time
    async with env.sf() as s:
        assert len((await s.execute(select(CallEvent))).scalars().all()) == 1
    assert len(env.redis.streams[STREAM]) == 1


async def test_database_failure_does_not_stop_the_greeting(env: Env) -> None:
    def broken() -> Any:
        raise RuntimeError("db down")

    app.state.session_factory = broken
    r = await env.post("/webhooks/twilio/voice", FORM)
    assert r.status_code == 200
    assert GREETING in r.text


async def test_unsigned_request_is_rejected(env: Env) -> None:
    r = await env.client.post("/webhooks/twilio/voice", data=FORM)
    assert r.status_code == 403


async def test_call_from_other_twilio_account_is_rejected(env: Env) -> None:
    r = await env.post("/webhooks/twilio/voice", {**FORM, "AccountSid": "AC" + "c" * 32})
    assert r.status_code == 403


async def test_malformed_call_sid_is_rejected(env: Env) -> None:
    r = await env.post("/webhooks/twilio/voice", {**FORM, "CallSid": "not-a-sid"})
    assert r.status_code == 400


async def test_webhooks_hidden_from_openapi(env: Env) -> None:
    paths = (await env.client.get("/openapi.json")).json()["paths"]
    assert not [p for p in paths if p.startswith("/webhooks")]


async def test_stream_twiml_when_enabled(env: Env) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(twilio_stream_enabled=True)
    r = await env.post("/webhooks/twilio/voice", FORM)
    assert 'url="wss://calls.example.com/ws/media"' in r.text
    token = r.text.split('name="token" value="')[1].split('"')[0]
    assert verify_stream_token(SID, token, TOKEN)
    assert not verify_stream_token("CA" + "d" * 32, token, TOKEN)


async def test_status_completed_updates_call_and_publishes(env: Env) -> None:
    await env.post("/webhooks/twilio/voice", FORM)
    r = await env.post(
        "/webhooks/twilio/status", {**FORM, "CallStatus": "completed", "CallDuration": "9"}
    )
    assert r.status_code == 204
    async with env.sf() as s:
        call = (await s.execute(select(Call))).scalar_one()
        assert call.status == "completed"
        assert call.duration_seconds == 9
        assert call.ended_at is not None
    assert [f["type"] for _id, f in env.redis.streams[STREAM]] == ["call.started", "call.completed"]


async def test_repeated_status_webhook_is_saved_once(env: Env) -> None:
    form = {**FORM, "CallStatus": "completed", "CallDuration": "9"}
    await env.post("/webhooks/twilio/status", form)
    r = await env.post("/webhooks/twilio/status", form)
    assert r.status_code == 204
    assert len(env.redis.streams[STREAM]) == 1


async def test_status_with_bad_values_is_rejected(env: Env) -> None:
    assert (
        await env.post("/webhooks/twilio/status", {**FORM, "CallStatus": "hacked"})
    ).status_code == 400
    bad = {**FORM, "CallStatus": "completed", "CallDuration": "9; DROP"}
    assert (await env.post("/webhooks/twilio/status", bad)).status_code == 400
    assert (await env.client.post("/webhooks/twilio/status", data=FORM)).status_code == 403


async def test_status_save_failure_returns_500_and_allows_retry(env: Env) -> None:
    good = app.state.session_factory

    def broken() -> Any:
        raise RuntimeError("db down")

    app.state.session_factory = broken
    form = {**FORM, "CallStatus": "completed"}
    with pytest.raises(RuntimeError):
        await env.post("/webhooks/twilio/status", form)
    app.state.session_factory = good
    assert (await env.post("/webhooks/twilio/status", form)).status_code == 204


def test_mask_number() -> None:
    assert mask_number("+15551230000") == "***0000"
    assert mask_number("") == "***"
