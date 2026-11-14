"""GPU speech-to-text server (faster-whisper), OpenAI-compatible.

    POST /v1/audio/transcriptions   (multipart: file, model, language, response_format)
    GET  /health

Run:  uvicorn stt_server:app --host 0.0.0.0 --port 8002   (from the model_server folder)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from common import AudioError, add_cuda_dll_dirs, decode_audio, env, gpu_info, require_key

add_cuda_dll_dirs()  # before faster-whisper loads its CUDA libraries

log = logging.getLogger("stt")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MODEL = env("WHISPER_MODEL", "small")
DEVICE = env("WHISPER_DEVICE", "cuda")
COMPUTE = env("WHISPER_COMPUTE_TYPE", "int8_float16")
DEFAULT_LANG = env("WHISPER_LANGUAGE") or None  # None = auto-detect
BEAM = int(env("WHISPER_BEAM_SIZE", "3"))
# Hint about the topic of the calls. Helps with names, times and dates. A request can override it.
DEFAULT_PROMPT = env("WHISPER_PROMPT") or None
MAX_MB = int(env("MAX_UPLOAD_MB", "25"))
# Languages the callers may speak, e.g. "en,hi,gu". With two or more, the language is detected for
# every utterance (the client's `language` is ignored); the FIRST one is the default.
LANGS = [x.strip() for x in env("WHISPER_LANGUAGES").split(",") if x.strip()]
SWITCH_PROB = float(env("WHISPER_SWITCH_PROB", "0.5"))  # how sure Whisper must be to leave the default

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


def _pick_language(model, audio, requested):
    """The language of this utterance, among LANGS. Short words like "yes" are hard to place, so
    the default language wins unless another one is clearly more likely."""
    if len(LANGS) < 2:
        return requested or DEFAULT_LANG
    try:
        probs = dict(model.detect_language(audio)[2])
    except Exception:  # detection must never lose the call
        log.exception("language detection failed")
        return LANGS[0]
    best = max(LANGS, key=lambda lang: probs.get(lang, 0.0))
    if best != LANGS[0] and probs.get(best, 0.0) < SWITCH_PROB:
        return LANGS[0]
    return best


def _drop_prompt_echo(text, prompt):
    """Whisper sometimes repeats its hint text on noise. Remove sentences made of hint words."""
    if not prompt or not text:
        return text
    words = set(re.findall(r"[\w']+", prompt.lower()))
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        tokens = re.findall(r"[\w']+", sentence.lower())
        if len(tokens) >= 3 and sum(t in words for t in tokens) / len(tokens) >= 0.8:
            log.warning("dropped a sentence that only repeats the hint: %r", sentence)
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def _transcribe(audio, language, prompt):
    model = get_model()
    with _lock:  # one GPU inference at a time
        lang = _pick_language(model, audio, language)
        hint = (prompt or DEFAULT_PROMPT) if lang in (None, "en") else None  # the hint is English
        segments, info = model.transcribe(
            audio,
            language=lang,
            beam_size=BEAM,
            initial_prompt=hint,
            temperature=0.0,  # no random retries: same audio, same text
            condition_on_previous_text=False,
            without_timestamps=True,  # we do not need them, and decoding is faster
            vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return _drop_prompt_echo(text, hint), info


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
    prompt: str | None = Form(default=None),
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
        text, info = await run_in_threadpool(_transcribe, audio, language, prompt)
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
