"""Shared helpers for the GPU model servers (settings, auth, audio).

This folder is deployed on the GPU machine on its own. It must not import anything from
the `app/` package.
"""

from __future__ import annotations

import io
import os
import secrets
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import Header, HTTPException

HERE = Path(__file__).resolve().parent


def load_env() -> None:
    """Load model_server/.env without overriding real environment variables."""
    path = HERE / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


API_KEY = env("MODEL_SERVER_API_KEY")


def require_key(authorization: str | None = Header(default=None)) -> None:
    """OpenAI-style auth: `Authorization: Bearer <MODEL_SERVER_API_KEY>`."""
    if not API_KEY:
        raise HTTPException(503, "MODEL_SERVER_API_KEY is not set on the model server")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, API_KEY):
        raise HTTPException(401, "invalid API key")


def add_cuda_dll_dirs() -> None:
    """Windows: make the CUDA DLLs installed by `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`
    findable. Without this, loading Whisper works but transcribing fails with
    "Library cublas64_12.dll is not found". Harmless elsewhere."""
    import glob
    import importlib.util

    if os.name != "nt":
        # Linux: load the libraries from `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`
        # into the process first, so CTranslate2 (faster-whisper) finds them without
        # LD_LIBRARY_PATH. Harmless if they are not installed (system CUDA is used then).
        import ctypes

        spec = importlib.util.find_spec("nvidia")
        roots = list(spec.submodule_search_locations or []) if spec else []
        for name in ("libcublasLt.so.12", "libcublas.so.12", "libcudnn.so.9"):
            for root in roots:
                for path in glob.glob(os.path.join(root, "*", "lib", name)):
                    try:
                        ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass
        return

    folders: list[str] = []
    spec = importlib.util.find_spec("nvidia")
    for root in list(spec.submodule_search_locations or []) if spec else []:
        folders += glob.glob(os.path.join(root, "*", "bin"))
    local_env = HERE / "bin" / "env" / "Library" / "bin"  # conda-forge CUDA libs (see README)
    if local_env.is_dir():
        folders.append(str(local_env))
    torch_spec = importlib.util.find_spec("torch")  # a CUDA build of PyTorch carries cuBLAS/cuDNN
    if torch_spec and torch_spec.submodule_search_locations:
        folders += glob.glob(os.path.join(list(torch_spec.submodule_search_locations)[0], "lib"))
    for folder in folders:
        os.add_dll_directory(folder)
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def gpu_info() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
        name, total, used = [x.strip() for x in out.split(",")]
        return {"name": name, "memory_total_mb": int(total), "memory_used_mb": int(used)}
    except Exception:
        return None


# ---------------- audio ----------------
class AudioError(ValueError):
    pass


def resample(x: np.ndarray, sr: int, target: int) -> np.ndarray:
    if sr == target:
        return x
    if sr > target:  # low-pass before downsampling to avoid aliasing
        n = np.arange(-32, 33)
        fir = np.sinc(2 * (0.45 * target / sr) * n) * np.hamming(len(n))
        x = np.convolve(x, fir / fir.sum(), mode="same")
    n_out = max(1, int(round(len(x) * target / sr)))
    return np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)


def decode_audio(data: bytes, target_sr: int = 16_000, ffmpeg: str = "ffmpeg") -> np.ndarray:
    """Any audio file -> mono float32 at target_sr. Falls back to ffmpeg for webm/mp3/m4a."""
    if not data:
        raise AudioError("empty audio file")
    try:
        samples, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        audio = resample(samples.mean(axis=1), int(sr), target_sr)
    except Exception:
        try:
            p = subprocess.run(
                [ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1",
                 "-ar", str(target_sr), "pipe:1"],
                input=data, capture_output=True, timeout=60, check=False)
        except FileNotFoundError as exc:
            raise AudioError("unreadable audio (and ffmpeg is not installed)") from exc
        if p.returncode != 0:
            raise AudioError("could not decode audio: " + p.stderr.decode("utf-8", "replace")[:200])
        audio = np.frombuffer(p.stdout, dtype=np.float32).copy()
    if audio.size == 0:
        raise AudioError("audio contains no samples")
    return np.clip(audio, -1.0, 1.0)


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def to_wav(x: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, x, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()
