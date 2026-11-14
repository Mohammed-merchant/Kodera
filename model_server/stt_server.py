"""OpenAI-style transcription endpoint backed by Gnani Prisma."""
import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from model_server.gnani_config import setting

app = FastAPI(title="Gnani STT")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), model: str = Form(""),
                         language: str = Form(""), response_format: str = Form("json")):
    key = setting("GNANI_API_KEY")
    if not key or key.startswith("put_your_"):
        raise HTTPException(503, "Set GNANI_API_KEY in .env")
    audio = await file.read(25 * 1024 * 1024 + 1)
    if not audio or len(audio) > 25 * 1024 * 1024:
        raise HTTPException(400, "Audio must be nonempty and at most 25 MB")
    lang = {"en": "en-IN", "hi": "hi-IN", "gu": "gu-IN"}.get(language, language or "indic-auto")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                setting("GNANI_STT_URL", "https://api.vachana.ai/stt/v3"),
                headers={"X-API-Key-ID": key},
                data={"model": setting("GNANI_STT_MODEL", "prisma-v3.0"), "language_code": lang},
                files={"audio_file": (file.filename or "audio.webm", audio, file.content_type or "audio/webm")},
            )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Gnani STT request failed; check your key and provider settings") from exc
    text = result.get("transcript", result.get("text", ""))
    if response_format == "text":
        return PlainTextResponse(text)
    return {"text": text, "language": result.get("detected_language_code", lang)}
