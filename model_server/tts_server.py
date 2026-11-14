"""OpenAI-style speech endpoint backed by Gnani Timbre."""
import io
import wave

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from model_server.gnani_config import setting

app = FastAPI(title="Gnani TTS")


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=4000)
    model: str = ""
    voice: str = ""
    language: str = "hi-IN"
    response_format: str = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    key = setting("GNANI_API_KEY")
    if not key or key.startswith("put_your_"):
        raise HTTPException(503, "Set GNANI_API_KEY in .env")
    if req.response_format not in {"wav", "pcm"}:
        raise HTTPException(400, "response_format must be wav or pcm")
    payload = {"text": req.input, "language": req.language,
               "model": setting("GNANI_TTS_MODEL", "timbre-v3.0"),
               "voice": req.voice or setting("GNANI_TTS_VOICE", "Shivani"), "speed": req.speed,
               "audio_config": {"encoding": "linear_pcm", "container": "wav", "num_channels": 1,
                                "sample_rate": 24000, "sample_width": 2}}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(setting("GNANI_TTS_URL", "https://api.vachana.ai/api/v1/tts/inference"),
                                         headers={"X-API-Key-ID": key}, json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Gnani TTS request failed; check your key, language and voice") from exc
    data = response.content
    if not data:
        raise HTTPException(502, "Gnani TTS returned empty audio")
    if not data.startswith(b"RIFF"):
        raw = data
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(raw)
        data = buffer.getvalue()
    if req.response_format == "pcm":
        try:
            with wave.open(io.BytesIO(data), "rb") as audio:
                data = audio.readframes(audio.getnframes())
        except (wave.Error, EOFError) as exc:
            raise HTTPException(502, "Gnani TTS returned invalid WAV audio") from exc
    return Response(data, media_type="audio/wav" if req.response_format == "wav" else "application/octet-stream")
