"""LiveKit Agents services pointed at the GPU model server (OpenAI-compatible HTTP).

STT, LLM and TTS all go through the same server, so one key (LLM_API_KEY) is used.
"""

from typing import Literal

from livekit.agents import vad as lk_vad
from livekit.plugins import openai, silero

from app.core.config import Settings
from app.pipeline.prompts import build_stt_prompt

TTS_SAMPLE_RATE = 24_000  # the GPU TTS server returns raw PCM at 24 kHz
VAD_SAMPLE_RATE: Literal[8000] = 8_000  # phone audio; Silero runs natively at 8 kHz, no resampling

_vad: lk_vad.VAD | None = None


def create_stt(s: Settings) -> openai.STT:
    # detect_language: no language is sent, so the GPU server picks Hindi, Gujarati or English
    # for every utterance itself (WHISPER_LANGUAGES on the GPU machine).
    return openai.STT(
        model="whisper-1",
        detect_language=True,
        prompt=build_stt_prompt(s),
        base_url=s.stt_base_url,
        api_key=s.llm_api_key.get_secret_value(),
        use_realtime=False,
    )


def create_llm(s: Settings) -> openai.LLM:
    return openai.LLM(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key.get_secret_value(),
        temperature=0.0,
        # llama-server reads max_tokens; strict tool schemas are an OpenAI-only feature.
        extra_body={"max_tokens": s.llm_max_tokens},
        _strict_tool_schema=False,
    )


def create_tts(s: Settings) -> openai.TTS:
    # The server ignores the OpenAI voice name and uses its own Kokoro / Piper voice.
    return openai.TTS(
        model="tts-1",
        voice="alloy",
        base_url=s.tts_base_url,
        api_key=s.llm_api_key.get_secret_value(),
        response_format="pcm",
        sample_rate=TTS_SAMPLE_RATE,
    )


def create_vad() -> lk_vad.VAD:
    """Silero VAD, loaded once per process (loading takes a moment) and shared by all calls."""
    global _vad
    if _vad is None:
        _vad = silero.VAD.load(sample_rate=VAD_SAMPLE_RATE)
    return _vad
