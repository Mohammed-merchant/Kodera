import base64
from collections.abc import Iterator
from typing import Any

import orjson
import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.ws import media as media_module
from app.core.config import Settings, get_settings
from app.core.security import stream_token
from app.main import app

TOKEN = "test-auth-token"
SID = "CA" + "e" * 32
PAYLOAD = base64.b64encode(b"\x7f" * 160).decode()
STOP = '{"event": "stop"}'


def start_msg(call_sid: str = SID, token: str | None = None) -> str:
    token = stream_token(call_sid, TOKEN) if token is None else token
    return orjson.dumps(
        {
            "event": "start",
            "start": {
                "callSid": call_sid,
                "streamSid": "MZ123",
                "customParameters": {"token": token},
            },
        }
    ).decode()


def media_msg(track: str = "inbound") -> str:
    return orjson.dumps({"event": "media", "media": {"track": track, "payload": PAYLOAD}}).decode()


def _settings() -> Settings:
    return Settings(_env_file=None, twilio_auth_token=SecretStr(TOKEN), max_call_seconds=5)


SAVED: list[tuple[str, str, dict[str, Any]]] = []


async def fake_record(
    session_factory: Any, call_sid: str, event_type: str, data: dict[str, Any]
) -> None:
    SAVED.append((call_sid, event_type, data))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    SAVED.clear()
    monkeypatch.setattr(media_module, "record_stream_event", fake_record)
    app.dependency_overrides[get_settings] = _settings
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def expect_rejected(ws: Any) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_text()
    assert exc.value.code == 1008


def test_audio_is_echoed_back_after_valid_start(client: TestClient) -> None:
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text('{"event": "connected", "protocol": "Call"}')
        ws.send_text(start_msg())
        ws.send_text(media_msg())
        echoed = orjson.loads(ws.receive_text())
        assert echoed == {"event": "media", "streamSid": "MZ123", "media": {"payload": PAYLOAD}}
        ws.send_text(STOP)


def test_outbound_track_is_not_echoed(client: TestClient) -> None:
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(start_msg())
        ws.send_text(media_msg(track="outbound"))
        ws.send_text(media_msg())
        echoed = orjson.loads(ws.receive_text())  # the first reply is for the inbound frame
        assert echoed["event"] == "media"
        ws.send_text(STOP)


def test_wrong_token_is_rejected(client: TestClient) -> None:
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(start_msg(token="forged"))
        expect_rejected(ws)


def test_token_for_another_call_is_rejected(client: TestClient) -> None:
    other = stream_token("CA" + "f" * 32, TOKEN)
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(start_msg(token=other))
        expect_rejected(ws)


def test_audio_before_start_is_rejected(client: TestClient) -> None:
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(media_msg())
        expect_rejected(ws)


@pytest.mark.parametrize("payload", ["not json", "x" * 20_000])
def test_garbage_and_oversized_messages_are_rejected(client: TestClient, payload: str) -> None:
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(payload)
        expect_rejected(ws)


def test_stream_events_are_saved(client: TestClient) -> None:
    with client.websocket_connect("/ws/media") as ws:
        ws.send_text(start_msg())
        ws.send_text(media_msg())
        ws.receive_text()
        ws.send_text(STOP)
    assert [(sid, kind) for sid, kind, _ in SAVED] == [
        (SID, "stream.started"),
        (SID, "stream.stopped"),
    ]
    assert SAVED[1][2] == {"frames": 1, "reason": "stop"}
