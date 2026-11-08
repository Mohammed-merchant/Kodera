import base64
from collections.abc import Iterator
from typing import Any

import orjson
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.ws import exotel_media
from app.core.config import Settings, get_settings
from app.main import app

KEY = "api-key"
TOKEN = "api-token"
ACCOUNT = "exotel_acct"
SID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"{KEY}:{TOKEN}".encode()).decode()}
STOP = orjson.dumps({"event": "stop", "stop": {"call_sid": SID, "reason": "callended"}}).decode()
SAVED: list[tuple[str, str, dict[str, Any]]] = []


def start_msg(call_sid: str = SID, account: str = ACCOUNT) -> str:
    return orjson.dumps(
        {
            "event": "start",
            "sequence_number": 1,
            "stream_sid": "ST123",
            "start": {
                "stream_sid": "ST123",
                "call_sid": call_sid,
                "account_sid": account,
                "from": "+919876543210",
                "to": "+918041234567",
                "custom_parameters": {},
                "media_format": {"encoding": "raw", "sample_rate": 8000, "bit_rate": 128},
            },
        }
    ).decode()


def media_msg(nbytes: int, fill: int = 1) -> str:
    payload = base64.b64encode(bytes([fill]) * nbytes).decode()
    return orjson.dumps(
        {"event": "media", "stream_sid": "ST123", "media": {"chunk": 1, "payload": payload}}
    ).decode()


async def fake_record(
    session_factory: Any, call_sid: str, event_type: str, data: dict[str, Any], provider: str = ""
) -> None:
    SAVED.append((call_sid, event_type, {**data, "provider": provider}))


def _settings(**extra: Any) -> Settings:
    values: dict[str, Any] = {
        "call_provider": "exotel",
        "exotel_api_key": SecretStr(KEY),
        "exotel_api_token": SecretStr(TOKEN),
        "exotel_account_sid": ACCOUNT,
        "max_call_seconds": 5,
        **extra,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    SAVED.clear()
    monkeypatch.setattr(exotel_media, "record_stream_event", fake_record)
    app.dependency_overrides[get_settings] = lambda: _settings()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def connect(client: TestClient, headers: dict[str, str] | None = None) -> Any:
    return client.websocket_connect("/ws/exotel", headers=AUTH if headers is None else headers)


def expect_rejected(ws: Any) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_text()
    assert exc.value.code == 1008


# --- handshake security -----------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic " + base64.b64encode(b"api-key:wrong").decode()},
        {"Authorization": "Basic " + base64.b64encode(b"other:api-token").decode()},
        {"Authorization": "Bearer api-token"},
        {"Authorization": "Basic not-base64!!"},
    ],
)
def test_handshake_without_valid_basic_auth_is_refused(
    client: TestClient, headers: dict[str, str]
) -> None:
    with pytest.raises(WebSocketDisconnect) as exc, connect(client, headers):
        pass
    assert exc.value.code == 1008


def test_handshake_refused_when_credentials_not_configured(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        call_provider="exotel",
        exotel_api_key=SecretStr(""),
        exotel_api_token=SecretStr(""),
    )
    empty = {"Authorization": "Basic " + base64.b64encode(b":").decode()}
    with pytest.raises(WebSocketDisconnect), connect(client, empty):
        pass


def test_handshake_refused_when_twilio_is_the_active_provider(client: TestClient) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(call_provider="twilio")
    with pytest.raises(WebSocketDisconnect) as exc, connect(client):
        pass
    assert exc.value.code == 1008


# --- stream behaviour -------------------------------------------------------------------


def test_audio_is_echoed_back_in_valid_chunks(client: TestClient) -> None:
    with connect(client) as ws:
        ws.send_text('{"event": "connected"}')
        ws.send_text(start_msg())
        for _ in range(4):  # 4 x 1000 bytes: the first reply is only sent at 3200 bytes
            ws.send_text(media_msg(1000))
        reply = orjson.loads(ws.receive_text())
        assert reply["event"] == "media"
        assert reply["stream_sid"] == "ST123"
        audio = base64.b64decode(reply["media"]["payload"])
        # 4000 bytes arrived: send back the largest multiple of 320 (3840), keep the rest.
        assert len(audio) == 3840 and len(audio) % 320 == 0
        assert audio == b"\x01" * 3840
        ws.send_text(STOP)


def test_no_reply_until_enough_audio(client: TestClient) -> None:
    with connect(client) as ws:
        ws.send_text(start_msg())
        ws.send_text(media_msg(1600))
        ws.send_text(media_msg(1600))  # now 3200 bytes buffered -> exactly one reply
        reply = orjson.loads(ws.receive_text())
        assert len(base64.b64decode(reply["media"]["payload"])) == 3200
        ws.send_text(STOP)
    assert SAVED[-1][2]["chunks_sent"] == 1


def test_broken_audio_frame_is_ignored_and_call_continues(client: TestClient) -> None:
    bad = orjson.dumps({"event": "media", "media": {"payload": "***"}}).decode()
    with connect(client) as ws:
        ws.send_text(start_msg())
        ws.send_text(bad)
        ws.send_text(media_msg(3200))
        assert orjson.loads(ws.receive_text())["event"] == "media"
        ws.send_text(STOP)


def test_dtmf_and_mark_events_are_ignored(client: TestClient) -> None:
    dtmf = orjson.dumps({"event": "dtmf", "dtmf": {"digit": "5", "duration": "100"}}).decode()
    mark = orjson.dumps({"event": "mark", "mark": {"name": "x"}}).decode()
    with connect(client) as ws:
        ws.send_text(start_msg())
        ws.send_text(dtmf)
        ws.send_text(mark)
        ws.send_text(media_msg(3200))
        assert orjson.loads(ws.receive_text())["event"] == "media"
        ws.send_text(STOP)


def test_audio_before_start_is_rejected(client: TestClient) -> None:
    with connect(client) as ws:
        ws.send_text(media_msg(3200))
        expect_rejected(ws)


def test_start_with_other_account_is_rejected(client: TestClient) -> None:
    with connect(client) as ws:
        ws.send_text(start_msg(account="someone_else"))
        expect_rejected(ws)


def test_start_with_bad_call_sid_is_rejected(client: TestClient) -> None:
    with connect(client) as ws:
        ws.send_text(start_msg(call_sid="bad sid;"))
        expect_rejected(ws)


@pytest.mark.parametrize("payload", ["not json", "[1]", "x" * 300_000])
def test_garbage_and_oversized_messages_are_rejected(client: TestClient, payload: str) -> None:
    with connect(client) as ws:
        ws.send_text(payload)
        expect_rejected(ws)


def test_stream_events_are_saved_with_the_stop_reason(client: TestClient) -> None:
    with connect(client) as ws:
        ws.send_text(start_msg())
        ws.send_text(media_msg(3200))
        ws.receive_text()
        ws.send_text(STOP)
    assert [(sid, kind) for sid, kind, _ in SAVED] == [
        (SID, "stream.started"),
        (SID, "stream.stopped"),
    ]
    stopped = SAVED[1][2]
    assert stopped["reason"] == "callended"
    assert stopped["frames"] == 1
    assert stopped["provider"] == "exotel"
