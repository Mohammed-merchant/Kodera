"""Start all three GPU services on this machine and stop them together on Ctrl+C.

    LLM  -> llama.cpp `llama-server` (Qwen GGUF)   port 8001   /v1/chat/completions
    STT  -> stt_server.py (faster-whisper)         port 8002   /v1/audio/transcriptions
    TTS  -> tts_server.py (Piper)                  port 8003   /v1/audio/speech
    gateway.py                                     port 8000   all of the above on ONE port (for ngrok)

    python run_all.py            # all three
    python run_all.py stt tts    # only some (e.g. when the LLM runs under Ollama instead)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from common import HERE, env

HOST = env("BIND_HOST", "127.0.0.1")  # ngrok/gateway reach it locally
KEY = env("MODEL_SERVER_API_KEY")


def llm_cmd() -> list[str]:
    exe = env("LLAMA_SERVER_BIN", "llama-server")
    if not shutil.which(exe):
        sys.exit(f"'{exe}' not found. Install llama.cpp and put llama-server on PATH, "
                 "or set LLAMA_SERVER_BIN. See model_server/README.md.")
    gguf = env("LLM_GGUF")
    if not gguf:
        sys.exit("LLM_GGUF is not set (path to the Qwen .gguf file) in model_server/.env")
    gguf_path = Path(gguf) if Path(gguf).is_absolute() else HERE / gguf
    if not gguf_path.is_file():
        sys.exit(f"Model file not found: {gguf_path}\nDownload it with: python download_models.py llm")
    cmd = [exe, "-m", str(gguf_path), "--host", HOST, "--port", env("LLM_PORT", "8001"),
           "-ngl", env("LLM_GPU_LAYERS", "99"), "-c", env("LLM_CTX", "2048"),
           "--alias", env("LLM_ALIAS", "qwen"), "--jinja",
           # Gentle on a 4 GB laptop GPU: one conversation at a time (not 4 KV caches), small
           # compute buffers, and a few CPU threads so the machine stays cool.
           # LLM_PARALLEL = how many calls the LLM serves at once. The context (LLM_CTX) is
           # shared between them: 2 calls of 4096 tokens need LLM_CTX=8192.
           "-np", env("LLM_PARALLEL", "1"), "-b", env("LLM_BATCH", "512"),
           "-ub", env("LLM_UBATCH", "256"),
           "--threads", env("LLM_THREADS", "4")]
    if KEY:
        cmd += ["--api-key", KEY]
    return cmd


def child_env(name: str) -> dict[str, str]:
    """Linux: llama-server's CUDA libraries sit next to it (and in LLM_LIB_DIRS)."""
    e = dict(os.environ)
    if name == "llm":
        exe = Path(env("LLAMA_SERVER_BIN", "llama-server"))
        dirs = [str(exe.resolve().parent)] if exe.exists() else []
        dirs += [d for d in env("LLM_LIB_DIRS").split(os.pathsep) if d]
        e["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + [e.get("LD_LIBRARY_PATH", "")]).strip(os.pathsep)
    elif name == "tts" and os.name != "nt":
        # Kokoro runs on onnxruntime-gpu, which needs the CUDA 12 libraries from the pip packages
        # nvidia-cublas-cu12 / nvidia-cudnn-cu12 (cuFFT/cuRAND/CUDA runtime come with them).
        import glob
        import importlib.util

        spec = importlib.util.find_spec("nvidia")
        roots = list(spec.submodule_search_locations or []) if spec else []
        libs = [d for r in roots for d in sorted(glob.glob(os.path.join(r, "*", "lib")))]
        e["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [e.get("LD_LIBRARY_PATH", "")]).strip(os.pathsep)
    return e


def uvicorn_cmd(module: str, port: str) -> list[str]:
    return [sys.executable, "-m", "uvicorn", f"{module}:app", "--host", HOST, "--port", port]


# ---- GPU guard: stop everything rather than push a laptop GPU too hard ----
MAX_TEMP_C = int(env("GPU_MAX_TEMP_C", "83"))
MAX_MEM_PERCENT = int(env("GPU_MAX_MEM_PERCENT", "92"))
GUARD_SECONDS = int(env("GUARD_INTERVAL_SECONDS", "10"))


def gpu_reading() -> tuple[int, int, int] | None:
    """(temperature C, memory used MiB, memory total MiB), or None if nvidia-smi is unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()[0]
        temp, used, total = (int(x) for x in out.split(","))
        return temp, used, total
    except Exception:
        return None


def gpu_problem(reading: tuple[int, int, int] | None) -> str | None:
    if reading is None:
        return None
    temp, used, total = reading
    if temp >= MAX_TEMP_C:
        return f"GPU temperature {temp} C is at or above the limit of {MAX_TEMP_C} C"
    if used * 100 >= total * MAX_MEM_PERCENT:
        return f"GPU memory {used}/{total} MiB is above {MAX_MEM_PERCENT}% of the card"
    return None


def wait_until_up(url: str, seconds: int, procs: dict[str, subprocess.Popen]) -> bool:
    """Wait for a service to answer 200, so heavy model loads never overlap."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        for name, p in procs.items():
            if p.poll() is not None:
                sys.exit(f"[run_all] {name} exited with code {p.returncode} while starting")
        problem = gpu_problem(gpu_reading())
        if problem:
            sys.exit(f"[run_all] STOPPING for safety: {problem}")
        try:
            if urllib.request.urlopen(url, timeout=3).status == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def main() -> None:
    if not KEY:
        sys.exit("Set MODEL_SERVER_API_KEY in model_server/.env first (the servers refuse to run open).")
    want = set(sys.argv[1:]) or {"llm", "stt", "tts", "gateway"}
    plan = {
        "llm": lambda: llm_cmd(),
        "stt": lambda: uvicorn_cmd("stt_server", env("STT_PORT", "8002")),
        "tts": lambda: uvicorn_cmd("tts_server", env("TTS_PORT", "8003")),
        "gateway": lambda: uvicorn_cmd("gateway", env("GATEWAY_PORT", "8000")),
    }
    unknown = want - plan.keys()
    if unknown:
        sys.exit(f"unknown service(s): {', '.join(sorted(unknown))}; choose from llm stt tts gateway")

    start = gpu_reading()
    if start and start[0] > MAX_TEMP_C - 10:
        sys.exit(f"[run_all] The GPU is already at {start[0]} C. Let it cool below {MAX_TEMP_C - 10} C first.")
    ready_urls = {  # a service counts as started when this answers 200
        "llm": f"http://127.0.0.1:{env('LLM_PORT', '8001')}/health",
        "stt": f"http://127.0.0.1:{env('STT_PORT', '8002')}/health",
        "tts": f"http://127.0.0.1:{env('TTS_PORT', '8003')}/health",
    }
    procs: dict[str, subprocess.Popen] = {}
    try:
        # One at a time, LLM first: the heavy model loads never overlap on the GPU.
        for name in ("llm", "stt", "tts", "gateway"):
            if name in want:
                cmd = plan[name]()
                print(f"[run_all] starting {name}: {' '.join(c for c in cmd if c != KEY)}", flush=True)
                procs[name] = subprocess.Popen(cmd, cwd=HERE, env=child_env(name))
                if name in ready_urls:
                    if not wait_until_up(ready_urls[name], 240, procs):
                        sys.exit(f"[run_all] {name} did not become ready in 240 s; stopping")
                    r = gpu_reading()
                    print(f"[run_all] {name} ready. GPU: {r[0]} C, {r[1]}/{r[2]} MiB" if r else f"[run_all] {name} ready", flush=True)
                else:
                    time.sleep(1)
        print("[run_all] all services are up; the guard checks the GPU every "
              f"{GUARD_SECONDS} s (limits {MAX_TEMP_C} C, {MAX_MEM_PERCENT}% memory)", flush=True)
        breaches, last_check = 0, 0.0
        while True:
            for name, p in procs.items():
                if p.poll() is not None:
                    sys.exit(f"[run_all] {name} exited with code {p.returncode}; stopping the rest")
            if time.time() - last_check >= GUARD_SECONDS:
                last_check = time.time()
                problem = gpu_problem(gpu_reading())
                breaches = breaches + 1 if problem else 0
                if breaches >= 2:  # two readings in a row, so one blip does not stop a call
                    sys.exit(f"[run_all] STOPPING for safety: {problem}")
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
