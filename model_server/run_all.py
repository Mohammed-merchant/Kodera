"""Start all three GPU services on this machine and stop them together on Ctrl+C.

    LLM  -> llama.cpp `llama-server` (Qwen GGUF)   port 8001   /v1/chat/completions
    STT  -> stt_server.py (faster-whisper)         port 8002   /v1/audio/transcriptions
    TTS  -> tts_server.py (Piper)                  port 8003   /v1/audio/speech
    gateway.py                                     port 8000   all of the above on ONE port (for ngrok)

    python run_all.py            # all three
    python run_all.py stt tts    # only some (e.g. when the LLM runs under Ollama instead)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
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
           "--alias", env("LLM_ALIAS", "qwen"), "--jinja"]
    if KEY:
        cmd += ["--api-key", KEY]
    return cmd


def uvicorn_cmd(module: str, port: str) -> list[str]:
    return [sys.executable, "-m", "uvicorn", f"{module}:app", "--host", HOST, "--port", port]


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

    procs: dict[str, subprocess.Popen] = {}
    try:
        # LLM first so its VRAM is reserved before Whisper loads.
        for name in ("llm", "stt", "tts", "gateway"):
            if name in want:
                cmd = plan[name]()
                print(f"[run_all] starting {name}: {' '.join(c for c in cmd if c != KEY)}")
                procs[name] = subprocess.Popen(cmd, cwd=HERE)
                time.sleep(3 if name == "llm" else 1)
        while True:
            for name, p in procs.items():
                if p.poll() is not None:
                    sys.exit(f"[run_all] {name} exited with code {p.returncode}; stopping the rest")
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
