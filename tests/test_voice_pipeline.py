from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.ws import exotel_media
from app.core.config import Settings, get_settings
from app.main import app
from app.pipeline import services
from app.pipeline.prompts import build_greeting, build_system_prompt
from app.pipeline.voice_pipeline import gpu_server_reachable
from tests.test_exotel_ws import (
    AUTH,
    SAVED,
    _settings,
    fake_record,
    start_msg,
)

CLOSED_PORT_URL = "http://127.0.0.1:9/v1"  # nothing listens on the discard port


def agent_settings(**extra: Any) -> Settings:
    return _settings(
        voice_agent_enabled=True,
        llm_base_url=CLOSED_PORT_URL,
        llm_api_key=SecretStr("k"),
        **extra,
    )


def test_prompts_use_the_agent_name() -> None:
    assert "Kodera" in build_system_prompt("Kodera")
    assert build_greeting("Hi, this is {name}.", "Kodera") == "Hi, this is Kodera."


def test_services_point_at_the_gpu_server() -> None:
    s = Settings(
        _env_file=None,
        llm_api_key=SecretStr("k"),
        llm_base_url="https://gpu.example/v1",
        stt_base_url="https://gpu.example/v1",
        tts_base_url="https://gpu.example/v1",
        llm_model="qwen",
    )
    for service in (services.create_stt(s), services.create_llm(s), services.create_tts(s)):
        assert str(service._client.base_url).startswith("https://gpu.example/v1")


async def test_gpu_server_reachable_is_false_when_down() -> None:
    assert await gpu_server_reachable(agent_settings()) is False


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    SAVED.clear()
    monkeypatch.setattr(exotel_media, "record_stream_event", fake_record)
    app.dependency_overrides[get_settings] = lambda: agent_settings()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_call_ends_cleanly_when_gpu_server_is_down(client: TestClient) -> None:
    with client.websocket_connect("/ws/exotel", headers=AUTH) as ws:
        ws.send_text(start_msg())
        with pytest.raises(WebSocketDisconnect):  # the server hangs up at once
            ws.receive_text()
    stopped = [d for _, event, d in SAVED if event == "stream.stopped"]
    assert [d["reason"] for d in stopped] == ["gpu unavailable"]
    assert [event for _, event, _ in SAVED][0] == "stream.started"
