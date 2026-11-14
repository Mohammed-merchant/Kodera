import httpx
from fastapi.testclient import TestClient

from model_server import stt_server, tts_server, voice_chat


def test_stt_gnani_request(monkeypatch):
    monkeypatch.setenv("GNANI_API_KEY", "test-key")
    real_client = httpx.AsyncClient

    def handler(request):
        assert request.headers["x-api-key-id"] == "test-key"
        assert b'indic-auto' in request.content
        return httpx.Response(200, json={"transcript": "Hello", "detected_language_code": "en-IN"})

    monkeypatch.setattr(stt_server.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    with TestClient(stt_server.app) as client:
        response = client.post("/v1/audio/transcriptions", files={"file": ("test.webm", b"audio", "audio/webm")})
    assert response.json() == {"text": "Hello", "language": "en-IN"}


def test_tts_wraps_pcm(monkeypatch):
    monkeypatch.setenv("GNANI_API_KEY", "test-key")
    real_client = httpx.AsyncClient

    def handler(request):
        assert b'gu-IN' in request.content
        return httpx.Response(200, content=b'\x00\x00' * 20)

    monkeypatch.setattr(tts_server.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    with TestClient(tts_server.app) as client:
        response = client.post("/v1/audio/speech", json={"input": "hello", "language": "gu-IN"})
    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")


def test_browser_chat_pipeline(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    real_client = httpx.AsyncClient

    def handler(request):
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "Hello", "language": "en-IN"})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "Hi!"}}]})
        assert b'en-IN' in request.content
        return httpx.Response(200, content=b'RIFFaudio')

    monkeypatch.setattr(voice_chat.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    with TestClient(voice_chat.app) as client:
        assert client.get("/").status_code == 200
        response = client.post("/api/chat", files={"file": ("test.webm", b"audio", "audio/webm")})
    assert response.status_code == 200
    assert response.json()["answer"] == "Hi!"
