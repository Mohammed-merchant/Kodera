"""Download the model files the GPU servers need. Uses only the standard library.

    python download_models.py            # Piper voice + Qwen GGUF (what .env asks for)
    python download_models.py voice      # only the Piper voice
    python download_models.py llm        # only the Qwen GGUF

Piper voice: PIPER_VOICE in .env (default en_US-lessac-medium) -> models/piper/
Qwen GGUF:   LLM_GGUF in .env must point at a file listed in LLM_FILES below.
Whisper downloads itself the first time stt_server.py starts.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from common import HERE, env

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
LLM_FILES = {
    # file name -> repo it lives in. Single-file Q4_K_M builds only.
    "qwen2.5-3b-instruct-q4_k_m.gguf": "Qwen/Qwen2.5-3B-Instruct-GGUF",
    "qwen2.5-1.5b-instruct-q4_k_m.gguf": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
}


def fetch(url: str, dest: Path) -> None:
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"already have {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {url}")

    def progress(blocks: int, size: int, total: int) -> None:
        if total > 0:
            print(f"\r  {min(100, blocks * size * 100 // total)}%  ({total // 1_048_576} MB)",
                  end="", flush=True)

    urllib.request.urlretrieve(url, tmp, progress)
    tmp.replace(dest)
    print(f"\r  saved {dest}")


def voice() -> None:
    name = env("PIPER_VOICE", "en_US-lessac-medium")
    try:
        locale, speaker, quality = name.split("-")
    except ValueError:
        sys.exit(f"PIPER_VOICE={name!r} should look like en_US-lessac-medium")
    folder = Path(env("PIPER_VOICE_DIR", "models/piper"))
    folder = folder if folder.is_absolute() else HERE / folder
    base = f"{PIPER_BASE}/{locale.split('_')[0]}/{locale}/{speaker}/{quality}/{name}"
    fetch(f"{base}.onnx", folder / f"{name}.onnx")
    fetch(f"{base}.onnx.json", folder / f"{name}.onnx.json")


def llm() -> None:
    gguf = env("LLM_GGUF", "models/qwen2.5-3b-instruct-q4_k_m.gguf")
    path = Path(gguf) if Path(gguf).is_absolute() else HERE / gguf
    repo = LLM_FILES.get(path.name.lower())
    if not repo:
        sys.exit(f"Don't know where to download {path.name}. Known: {', '.join(LLM_FILES)}. "
                 "For other models download the .gguf yourself and set LLM_GGUF.")
    fetch(f"https://huggingface.co/{repo}/resolve/main/{path.name}", path)


if __name__ == "__main__":
    want = set(sys.argv[1:]) or {"voice", "llm"}
    if "voice" in want:
        voice()
    if "llm" in want:
        llm()
    print("done")
