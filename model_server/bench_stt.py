"""Compare Whisper models on speed and accuracy, on THIS machine. Run on the GPU laptop.

    python bench_stt.py                                  # small, small.en, distil-large-v3
    python bench_stt.py --models small.en large-v3-turbo # your own list

It makes test calls with the Piper voice (clinic phrases with times and dates, squeezed to
phone quality: 8 kHz, a little noise), runs every model with and without the topic prompt,
and prints the average time per call and the word error rate (lower is better).
TTS speech is cleaner than real callers, so use the numbers to compare models, not as the
accuracy you will see on real calls. Models download on first use (up to ~1.5 GB each).
"""

from __future__ import annotations

import argparse
import re
import time

import numpy as np

from common import add_cuda_dll_dirs, env, resample

add_cuda_dll_dirs()

PHRASES = [
    "I want an appointment for Thursday 12 November at 11:15 AM",
    "Can I come tomorrow evening at 5:30 PM",
    "My name is Asha Rao and I have a fever and a cough",
    "Please cancel my appointment on Friday",
    "What are the doctor's timings and the consultation fees",
    "Reschedule it to Monday morning at 10 AM please",
    "Yes, please confirm the booking for 6:15 PM",
]
PROMPT = (
    "Phone call to a clinic. The caller books, moves or cancels an appointment. "
    "Times like 11:15 AM, 5:30 PM. Dates like Thursday 12 November, tomorrow. "
    "Words: appointment, reschedule, cancel, consultation, fees."
)


def normalize(text: str) -> list[str]:
    text = text.lower().replace("a.m.", "am").replace("p.m.", "pm")
    return re.sub(r"[^a-z0-9: ]", " ", text).split()


def wer(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[len(hyp)], len(ref)


def make_calls() -> list[tuple[str, np.ndarray]]:
    from tts_server import _synth, get_voice

    voice = get_voice(env("PIPER_VOICE", "en_US-lessac-medium"))
    rng = np.random.default_rng(0)
    calls = []
    for phrase in PHRASES:
        audio, sr = _synth(voice, phrase)
        phone = resample(resample(audio, sr, 8000), 8000, 16000)  # phone bandwidth
        phone = phone + rng.normal(0, 0.004, phone.shape).astype(np.float32)
        calls.append((phrase, phone.astype(np.float32)))
    return calls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["small", "small.en", "distil-large-v3"])
    ap.add_argument("--device", default=env("WHISPER_DEVICE", "cuda"))
    ap.add_argument("--compute", default=env("WHISPER_COMPUTE_TYPE", "int8_float16"))
    ap.add_argument("--beam", type=int, default=int(env("WHISPER_BEAM_SIZE", "3")))
    a = ap.parse_args()

    from faster_whisper import WhisperModel

    calls = make_calls()
    print(f"{len(calls)} test calls, beam={a.beam}, {a.device}/{a.compute}\n")
    print(f"{'model':<18}{'prompt':<8}{'avg ms':>8}{'WER %':>8}")
    for name in a.models:
        try:
            model = WhisperModel(name, device=a.device, compute_type=a.compute)
        except Exception as exc:
            print(f"{name:<18} could not load: {type(exc).__name__}: {exc}")
            continue
        model.transcribe(calls[0][1], language="en")  # warm-up
        for use_prompt in (False, True):
            errors = words = 0
            t0 = time.perf_counter()
            for ref, audio in calls:
                segs, _ = model.transcribe(
                    audio, language="en", beam_size=a.beam, temperature=0.0,
                    initial_prompt=PROMPT if use_prompt else None,
                    condition_on_previous_text=False, without_timestamps=True, vad_filter=True,
                )
                e, n = wer(normalize(ref), normalize(" ".join(s.text for s in segs)))
                errors, words = errors + e, words + n
            ms = (time.perf_counter() - t0) * 1000 / len(calls)
            print(f"{name:<18}{'yes' if use_prompt else 'no':<8}{ms:>8.0f}{100 * errors / words:>8.1f}")
        del model


if __name__ == "__main__":
    main()
