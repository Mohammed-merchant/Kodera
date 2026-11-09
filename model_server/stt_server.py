"""GPU speech-to-text server (faster-whisper), OpenAI-compatible.

    POST /v1/audio/transcriptions   (multipart: file, model, language, response_format)
    GET  /health

Run:  uvicorn stt_server:app --host 0.0.0.0 --port 8002   (from the model_server folder)
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from common import AudioError, decode_audio, env, gpu_info, require_key

log = logging.getLogger("stt")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MODEL = env("WHISPER_MODEL", "small")
DEVICE = env("WHISPER_DEVICE", "cuda")
COMPUTE = env("WHISPER_COMPUTE_TYPE", "int8_float16")
DEFAULT_LANG = env("WHISPER_LANGUAGE") or None  # None = auto-detect
BEAM = int(env("WHISPER_BEAM_SIZE", "1"))
MAX_MB = int(env("MAX_UPLOAD_MB", "25"))

_model = None
_lock = threading.Lock()


def get_model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel  # needs the GPU extras installed

            log.info("loading faster-whisper %s on %s (%s)", MODEL, DEVICE, COMPUTE)
            t0 = time.perf_counter()
            _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE)
            log.info("model loaded in %.1fs", time.perf_counter() - t0)
    return _model


@asynccontextmanager
async def lifespan(_: FastAPI):
    if env("PRELOAD", "true").lower() == "true":  # avoid a slow first phone call
        await run_in_threadpool(get_model)
    yield


app = FastAPI(title="STT server", lifespan=lifespan)


def _transcribe(audio, language):
    model = get_model()
    with _lock:  # one GPU inference at a time
        segments, info = model.transcribe(audio, language=language or DEFAULT_LANG,
                                          beam_size=BEAM, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip(), info


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "device": DEVICE, "compute_type": COMPUTE,
            "loaded": _model is not None, "gpu": gpu_info()}


@app.post("/v1/audio/transcriptions", dependencies=[Depends(require_key)])
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(default=""),  # accepted for OpenAI compatibility, ignored
    language: str | None = Form(default=None),
    response_format: str = Form(default="json"),
):
    data = await file.read(MAX_MB * 1024 * 1024 + 1)
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"file larger than {MAX_MB} MB")
    try:
        audio = await run_in_threadpool(decode_audio, data, 16_000)
    except AudioError as exc:
        raise HTTPException(400, str(exc)) from exc

    t0 = time.perf_counter()
    try:
        text, info = await run_in_threadpool(_transcribe, audio, language)
    except Exception as exc:
        log.exception("transcription failed")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
    log.info("stt in=%.2fs audio out=%d chars lang=%s took=%dms", audio.size / 16000, len(text),
             info.language, (time.perf_counter() - t0) * 1000)

    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "verbose_json":
        return {"text": text, "language": info.language, "duration": audio.size / 16000}
    return {"text": text}
