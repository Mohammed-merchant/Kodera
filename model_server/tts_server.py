"""GPU-machine text-to-speech server, OpenAI-compatible. Engines: Kokoro (default when its files
exist; a natural, warm voice that runs on the GPU) or Piper (CPU, the fallback).

    POST /v1/audio/speech   {model, input, voice, response_format: "pcm" | "wav"}
    GET  /health

`pcm` is raw 16-bit little-endian mono at 24000 Hz, exactly what OpenAI's API (and Pipecat's
OpenAI TTS service) uses. `wav` keeps the voice's native sample rate.
Kokoro-82M is small (about 0.3 GB of VRAM) and speaks a short sentence in ~250 ms on a T4.
Piper runs on CPU, costs no VRAM and is the fallback if Kokoro is not installed.

Run:  uvicorn tts_server:app --host 0.0.0.0 --port 8003   (from the model_server folder)
"""

from __future__ import annotations

import logging
import os
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
_kdir = Path(env("KOKORO_DIR", "models/kokoro"))
KOKORO_DIR = _kdir if _kdir.is_absolute() else HERE / _kdir
KOKORO_VOICE = env("KOKORO_VOICE", "af_heart")
KOKORO_SPEED = float(env("KOKORO_SPEED", "1.0"))
KOKORO_HINDI_VOICE = env("KOKORO_HINDI_VOICE", "hf_alpha")
_mdir = Path(env("MMS_GUJARATI_DIR", "models/mms-guj"))  # Meta MMS-TTS, the Gujarati voice
MMS_DIR = _mdir if _mdir.is_absolute() else HERE / _mdir
_kokoro_files = (KOKORO_DIR / "kokoro-v1.0.onnx", KOKORO_DIR / "voices-v1.0.bin")
ENGINE = env("TTS_ENGINE", "kokoro" if all(f.is_file() for f in _kokoro_files) else "piper")
_VOICE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")  # blocks path traversal

_voices: dict[str, object] = {}
_lock = threading.Lock()
_kokoro: object | None = None


def get_kokoro():
    """Load Kokoro once. It uses the GPU when onnxruntime-gpu can find CUDA, else the CPU."""
    global _kokoro
    with _lock:
        if _kokoro is None:
            import onnxruntime

            if "CUDAExecutionProvider" in onnxruntime.get_available_providers():
                os.environ.setdefault("ONNX_PROVIDER", "CUDAExecutionProvider")  # else it uses the CPU
            from kokoro_onnx import Kokoro

            log.info("loading Kokoro from %s", KOKORO_DIR)
            _kokoro = Kokoro(str(_kokoro_files[0]), str(_kokoro_files[1]))
            log.info("Kokoro providers: %s", _kokoro.sess.get_providers())  # type: ignore[attr-defined]
        return _kokoro


def detect_language(text: str) -> str:
    """'hi' (Devanagari), 'gu' (Gujarati script) or 'en': the voice follows the script of the text."""
    hindi = sum("ऀ" <= c <= "ॿ" for c in text)
    gujarati = sum("઀" <= c <= "૿" for c in text)
    if gujarati > hindi:
        return "gu"
    return "hi" if hindi else "en"


def _synth_kokoro(text: str, voice: str, lang: str = "en-us") -> tuple[np.ndarray, int]:
    engine = get_kokoro()
    with _lock:
        audio, sr = engine.create(text, voice=voice, speed=KOKORO_SPEED, lang=lang)  # type: ignore[attr-defined]
    return audio.astype(np.float32), int(sr)


_mms: tuple[object, object] | None = None


def mms_available() -> bool:
    return (MMS_DIR / "model.safetensors").is_file()


def _synth_gujarati(text: str) -> tuple[np.ndarray, int]:
    global _mms
    import torch

    with _lock:
        if _mms is None:
            from transformers import AutoTokenizer, VitsModel

            log.info("loading MMS Gujarati from %s", MMS_DIR)
            _mms = (VitsModel.from_pretrained(str(MMS_DIR)).eval(), AutoTokenizer.from_pretrained(str(MMS_DIR)))
        model, tokenizer = _mms
        inputs = tokenizer(text, return_tensors="pt")
        with torch.no_grad():
            wave = model(**inputs).waveform[0].numpy()  # type: ignore[operator]
        return wave.astype(np.float32), int(model.config.sampling_rate)  # type: ignore[attr-defined]


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
    if env("PRELOAD", "true").lower() == "true" and ENGINE == "kokoro":
        await run_in_threadpool(get_kokoro)
        await run_in_threadpool(_synth_kokoro, "Warming up.", KOKORO_VOICE)  # first call is slow
        await run_in_threadpool(_synth_kokoro, "नमस्ते।", KOKORO_HINDI_VOICE, "hi")
        if mms_available():
            await run_in_threadpool(_synth_gujarati, "નમસ્તે")
    elif env("PRELOAD", "true").lower() == "true":
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
    return {"status": "ok", "engine": ENGINE,
            "default_voice": KOKORO_VOICE if ENGINE == "kokoro" else DEFAULT_VOICE, "loaded_voices": sorted(_voices),
            "voice_dir": str(VOICE_DIR),
            "voices_on_disk": sorted(p.stem for p in VOICE_DIR.glob("*.onnx")) if VOICE_DIR.is_dir() else [],
            "gpu": gpu_info()}


@app.post("/v1/audio/speech", dependencies=[Depends(require_key)])
async def speech(req: SpeechRequest):
    if req.response_format not in {"pcm", "wav"}:
        raise HTTPException(400, "response_format must be 'pcm' or 'wav'")
    t0 = time.perf_counter()
    if ENGINE == "kokoro":
        known = await run_in_threadpool(lambda: set(get_kokoro().get_voices()))  # type: ignore[attr-defined]
        language = detect_language(req.input)
        if language == "gu" and mms_available():
            name, job = "mms-guj", (_synth_gujarati, req.input)
        elif language in ("hi", "gu"):  # no Gujarati voice installed: the Hindi one is the closest
            name, job = KOKORO_HINDI_VOICE, (_synth_kokoro, req.input, KOKORO_HINDI_VOICE, language)
        else:
            name = req.voice if req.voice in known else KOKORO_VOICE  # "alloy" etc. -> our voice
            job = (_synth_kokoro, req.input, name)
    else:
        name = req.voice if (req.voice and (VOICE_DIR / f"{req.voice}.onnx").is_file()) else DEFAULT_VOICE
        job = (_synth, await run_in_threadpool(get_voice, name), req.input)
    try:
        audio, sr = await run_in_threadpool(*job)
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
