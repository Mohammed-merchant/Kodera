"""GPU-machine text-to-speech server (Piper), OpenAI-compatible.

    POST /v1/audio/speech   {model, input, voice, response_format: "pcm" | "wav"}
    GET  /health

`pcm` is raw 16-bit little-endian mono at 24000 Hz, exactly what OpenAI's API (and Pipecat's
OpenAI TTS service) uses. `wav` keeps the voice's native sample rate.
Piper itself runs on CPU and is fast enough for real-time phone calls; it costs no VRAM.

Run:  uvicorn tts_server:app --host 0.0.0.0 --port 8003   (from the model_server folder)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from common import HERE, env, gpu_info, require_key, resample, to_pcm16, to_wav

log = logging.getLogger("tts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_VOICE = env("PIPER_VOICE", "en_US-lessac-medium")
_dir = Path(env("PIPER_VOICE_DIR", "models/piper"))
VOICE_DIR = _dir if _dir.is_absolute() else HERE / _dir  # relative paths are from this folder
PCM_RATE = 24_000
_VOICE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")  # blocks path traversal

_voices: dict[str, object] = {}
_lock = threading.Lock()


def get_voice(name: str):
    if not _VOICE_NAME.match(name):
        raise HTTPException(400, f"invalid voice name {name!r}")
    with _lock:
        if name not in _voices:
            path = VOICE_DIR / f"{name}.onnx"
            if not path.is_file():
                raise HTTPException(404, f"voice not found: {path}")
            from piper import PiperVoice  # needs the GPU extras installed

            log.info("loading voice %s", path)
            _voices[name] = PiperVoice.load(str(path))
        return _voices[name]


@asynccontextmanager
async def lifespan(_: FastAPI):
    if env("PRELOAD", "true").lower() == "true":
        path = VOICE_DIR / f"{DEFAULT_VOICE}.onnx"
        if not path.is_file():
            raise RuntimeError(
                f"Piper voice file not found: {path}\n"
                "Download it with:  python download_models.py voice"
            )
        await run_in_threadpool(get_voice, DEFAULT_VOICE)
    yield


app = FastAPI(title="TTS server", lifespan=lifespan)


class SpeechRequest(BaseModel):
    model: str = ""  # accepted for compatibility, ignored
    input: str = Field(min_length=1, max_length=4000)
    voice: str = ""  # OpenAI voice names like "alloy" fall back to the default voice
    response_format: str = "pcm"
    speed: float = 1.0


def _synth(voice, text: str) -> tuple[np.ndarray, int]:
    with _lock:
        if hasattr(voice, "synthesize_stream_raw"):  # piper-tts 1.2.x
            pcm = np.frombuffer(b"".join(voice.synthesize_stream_raw(text)), dtype=np.int16)
            sr = voice.config.sample_rate
        else:  # piper-tts 1.3+
            chunks = list(voice.synthesize(text))
            if not chunks:
                raise RuntimeError("Piper produced no audio")
            sr = chunks[0].sample_rate
            pcm = np.concatenate([c.audio_int16_array for c in chunks])
    return pcm.astype(np.float32) / 32768.0, sr


@app.get("/health")
def health():
    return {"status": "ok", "default_voice": DEFAULT_VOICE, "loaded_voices": sorted(_voices),
            "voice_dir": str(VOICE_DIR),
            "voices_on_disk": sorted(p.stem for p in VOICE_DIR.glob("*.onnx")) if VOICE_DIR.is_dir() else [],
            "gpu": gpu_info()}


@app.post("/v1/audio/speech", dependencies=[Depends(require_key)])
async def speech(req: SpeechRequest):
    if req.response_format not in {"pcm", "wav"}:
        raise HTTPException(400, "response_format must be 'pcm' or 'wav'")
    name = req.voice if (req.voice and (VOICE_DIR / f"{req.voice}.onnx").is_file()) else DEFAULT_VOICE
    voice = await run_in_threadpool(get_voice, name)

    t0 = time.perf_counter()
    try:
        audio, sr = await run_in_threadpool(_synth, voice, req.input)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("synthesis failed")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
    log.info("tts voice=%s in=%d chars out=%.2fs audio took=%dms", name, len(req.input),
             audio.size / sr, (time.perf_counter() - t0) * 1000)

    if req.response_format == "wav":
        return Response(to_wav(audio, sr), media_type="audio/wav")
    return Response(to_pcm16(resample(audio, sr, PCM_RATE)), media_type="audio/pcm")
