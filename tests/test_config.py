import pytest

from app.core.config import Settings


def test_twilio_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")
    s = Settings(_env_file=None)
    assert s.twilio_account_sid == "ACtest"
    assert s.twilio_phone_number == "+15550000000"
    assert s.twilio_auth_token.get_secret_value() == "secret-token"
    assert "secret-token" not in repr(s)
