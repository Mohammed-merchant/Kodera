from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from twilio.request_validator import RequestValidator

from app.core.config import Settings, get_settings
from app.core.security import TwilioParams

TOKEN = "test-auth-token"
BASE = "https://industinternational.com"
FORM = {"CallSid": "CA123", "From": "+15551230000", "To": "+15550000000"}


def make_client(token: str = TOKEN) -> TestClient:
    app = FastAPI()

    @app.post("/webhooks/twilio/voice")
    async def voice(params: TwilioParams) -> dict[str, str]:
        return {"call_sid": params["CallSid"]}

    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, public_base_url=BASE, twilio_auth_token=SecretStr(token)
    )
    return TestClient(app)


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield make_client()


def sign(url: str, form: dict[str, str], token: str = TOKEN) -> str:
    signature: str = RequestValidator(token).compute_signature(url, form)
    return signature


def test_valid_signature_is_accepted(client: TestClient) -> None:
    sig = sign(f"{BASE}/webhooks/twilio/voice", FORM)
    r = client.post("/webhooks/twilio/voice", data=FORM, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200
    assert r.json() == {"call_sid": "CA123"}


def test_valid_signature_with_query_string(client: TestClient) -> None:
    sig = sign(f"{BASE}/webhooks/twilio/voice?x=1", FORM)
    r = client.post("/webhooks/twilio/voice?x=1", data=FORM, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200


def test_missing_signature_is_rejected(client: TestClient) -> None:
    assert client.post("/webhooks/twilio/voice", data=FORM).status_code == 403


def test_wrong_signature_is_rejected(client: TestClient) -> None:
    r = client.post("/webhooks/twilio/voice", data=FORM, headers={"X-Twilio-Signature": "nope"})
    assert r.status_code == 403


def test_signature_from_other_token_is_rejected(client: TestClient) -> None:
    sig = sign(f"{BASE}/webhooks/twilio/voice", FORM, token="other-token")
    r = client.post("/webhooks/twilio/voice", data=FORM, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 403


def test_tampered_body_is_rejected(client: TestClient) -> None:
    sig = sign(f"{BASE}/webhooks/twilio/voice", FORM)
    tampered = {**FORM, "From": "+19998887777"}
    r = client.post("/webhooks/twilio/voice", data=tampered, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 403


def test_wrong_host_in_signed_url_is_rejected(client: TestClient) -> None:
    sig = sign("https://evil.example.com/webhooks/twilio/voice", FORM)
    r = client.post("/webhooks/twilio/voice", data=FORM, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 403


def test_unconfigured_token_returns_503() -> None:
    c = make_client(token="")
    r = c.post("/webhooks/twilio/voice", data=FORM, headers={"X-Twilio-Signature": "x"})
    assert r.status_code == 503
