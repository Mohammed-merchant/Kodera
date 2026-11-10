"""End-to-end check of the three GPU services. Run it on the laptop (or from the app server).

    python smoke_test.py                       # localhost
    python smoke_test.py --host 192.168.1.42   # a remote GPU machine

Needs only httpx. The key comes from --key, MODEL_SERVER_API_KEY, or model_server/.env.
Steps: health of each service -> LLM reply -> TTS audio -> STT hears the TTS audio
("the quick brown fox") -> one full STT->LLM->TTS round with timings.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from common import API_KEY, env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--base", help="one URL for all three services, e.g. the ngrok URL")
    ap.add_argument("--key", default=API_KEY)
    ap.add_argument("--llm-port", default=env("LLM_PORT", "8001"))
    ap.add_argument("--stt-port", default=env("STT_PORT", "8002"))
    ap.add_argument("--tts-port", default=env("TTS_PORT", "8003"))
    ap.add_argument("--model", default=env("LLM_ALIAS", "qwen"))
    a = ap.parse_args()
    if not a.key:
        print("No key: set MODEL_SERVER_API_KEY in model_server/.env or pass --key")
        return 2

    llm = f"http://{a.host}:{a.llm_port}"
    stt = f"http://{a.host}:{a.stt_port}"
    tts = f"http://{a.host}:{a.tts_port}"
    if a.base:
        llm = stt = tts = a.base.rstrip("/")
    h = {"Authorization": f"Bearer {a.key}", "ngrok-skip-browser-warning": "1"}
    c = httpx.Client(timeout=120, headers=h)
    failed = 0

    def step(name: str, fn):
        nonlocal failed
        t0 = time.perf_counter()
        try:
            detail = fn()
            print(f"[OK]   {name}  ({(time.perf_counter() - t0) * 1000:.0f} ms)  {detail or ''}")
            return detail
        except Exception as exc:
            failed += 1
            detail = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else exc
            print(f"[FAIL] {name}: {type(exc).__name__}: {detail}")
            return None

    def chat(text: str) -> str:
        r = c.post(f"{llm}/v1/chat/completions", json={
            "model": a.model, "max_tokens": 60,
            "messages": [{"role": "system", "content": "Answer in one short spoken sentence."},
                         {"role": "user", "content": text}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def speak(text: str, fmt: str = "wav") -> bytes:
        r = c.post(f"{tts}/v1/audio/speech",
                   json={"model": "tts-1", "input": text, "voice": "default", "response_format": fmt})
        r.raise_for_status()
        return r.content

    def hear(wav: bytes) -> str:
        r = c.post(f"{stt}/v1/audio/transcriptions", files={"file": ("a.wav", wav)},
                   data={"model": "whisper-1"})
        r.raise_for_status()
        return r.json()["text"]

    def health(url: str):
        d = httpx.get(f"{url}/health", timeout=10, headers=h).json()
        gpu = d.get("gpu")
        return f"gpu={gpu['name']} {gpu['memory_used_mb']}/{gpu['memory_total_mb']} MB" if gpu else ""

    print("== 1. services are up ==")
    step("STT /health", lambda: health(stt))
    step("TTS /health", lambda: health(tts))
    step("LLM /v1/models", lambda: ", ".join(
        m["id"] for m in c.get(f"{llm}/v1/models").raise_for_status().json()["data"]))

    print("== 2. each service alone ==")
    reply = step("LLM chat", lambda: chat("Say hello in five words."))
    wav = step("TTS wav", lambda: f"{len(speak('the quick brown fox')) // 1024} KB")
    step("TTS pcm (24 kHz, what the app uses)",
         lambda: "ok" if len(speak("hello there", "pcm")) > 20_000 else 1 / 0)

    print("== 3. STT hears the TTS audio ==")
    audio = speak("the quick brown fox jumps over the lazy dog")

    def roundtrip():
        text = hear(audio).lower()
        missing = [w for w in ("quick", "brown", "fox") if w not in text]
        if missing:
            raise AssertionError(f"transcript {text!r} is missing {missing}")
        return f"heard: {text!r}"

    step("TTS -> STT round trip", roundtrip)

    print("== 4. one full turn: caller audio -> STT -> LLM -> TTS ==")
    t = {}

    def turn():
        t0 = time.perf_counter()
        text = hear(audio)
        t["stt"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        answer = chat(text)
        t["llm"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        speak(answer, "pcm")
        t["tts"] = time.perf_counter() - t0
        return " ".join(f"{k}={v * 1000:.0f}ms" for k, v in t.items()) + \
            f" total={sum(t.values()) * 1000:.0f}ms"

    step("full turn", turn)
    if t and sum(t.values()) > 3.0:
        print("       note: > 3 s per turn is slow for a phone call; see the tuning list in the guide")

    print("\nALL OK" if not failed else f"\n{failed} step(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
