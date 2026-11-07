from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from twilio.request_validator import RequestValidator

from app.api.routes.webhooks import GREETING, mask_number
from app.core.config import Settings, get_settings
from app.main import app

TOKEN = "test-auth-token"
BASE = "https://calls.example.com"
URL = f"{BASE}/webhooks/twilio/voice"
ACCOUNT = "AC" + "a" * 32
FORM = {
    "AccountSid": ACCOUNT,
    "CallSid": "CA" + "b" * 32,
    "From": "+15551230000",
    "To": "+15550000000",
    "Direction": "inbound",
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        public_base_url=BASE,
        twilio_auth_token=SecretStr(TOKEN),
        twilio_account_sid=ACCOUNT,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def post(client: TestClient, form: dict[str, str]) -> httpx.Response:
    sig = RequestValidator(TOKEN).compute_signature(URL, form)
    return client.post("/webhooks/twilio/voice", data=form, headers={"X-Twilio-Signature": sig})


def test_valid_call_gets_greeting_twiml(client: TestClient) -> None:
    r = post(client, FORM)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<Say>" in r.text and GREETING in r.text


def test_unsigned_request_is_rejected(client: TestClient) -> None:
    assert client.post("/webhooks/twilio/voice", data=FORM).status_code == 403


def test_call_from_other_twilio_account_is_rejected(client: TestClient) -> None:
    r = post(client, {**FORM, "AccountSid": "AC" + "c" * 32})
    assert r.status_code == 403


def test_malformed_call_sid_is_rejected(client: TestClient) -> None:
    r = post(client, {**FORM, "CallSid": "not-a-sid"})
    assert r.status_code == 400


def test_webhook_hidden_from_openapi(client: TestClient) -> None:
    assert "/webhooks/twilio/voice" not in client.get("/openapi.json").json()["paths"]


def test_mask_number() -> None:
    assert mask_number("+15551230000") == "***0000"
    assert mask_number("") == "***"
