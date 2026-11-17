"""LiveKit Agents services pointed at the GPU model server (OpenAI-compatible HTTP).

STT, LLM and TTS all go through the same server, so one key (LLM_API_KEY) is used.
"""

from typing import Literal

import httpx2 as httpx  # the HTTP package the OpenAI SDK (v3) and LiveKit plugin use
import openai as openai_sdk
from livekit.agents import vad as lk_vad
from livekit.plugins import openai, silero

from app.core.config import Settings
from app.pipeline.prompts import build_stt_prompt

TTS_SAMPLE_RATE = 24_000  # the GPU TTS server returns raw PCM at 24 kHz
VAD_SAMPLE_RATE: Literal[8000] = 8_000  # phone audio; Silero runs natively at 8 kHz, no resampling

_vad: lk_vad.VAD | None = None
_clients: dict[tuple[str, str], openai_sdk.AsyncClient] = {}
_http: dict[str, httpx.AsyncClient] = {}


def http_client(base_url: str) -> httpx.AsyncClient:
    """One connection pool per GPU server, shared by every call (STT, LLM, TTS and the health
    check). Opening a connection to the GPU server costs several round trips (TCP + TLS), which
    is slow over a long distance; reusing warm connections removes that from each turn."""
    root = base_url.removesuffix("/").removesuffix("/v1")
    if root not in _http:
        _http[root] = httpx.AsyncClient(
            # The same limits as the LiveKit OpenAI plugin; idle connections are kept longer.
            timeout=httpx.Timeout(connect=15.0, read=5.0, write=5.0, pool=5.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=50, max_keepalive_connections=50, keepalive_expiry=300
            ),
        )
    return _http[root]


def _client(base_url: str, api_key: str) -> openai_sdk.AsyncClient:
    """A shared OpenAI client. LiveKit does not close a client it was given, so ending one call
    never breaks another."""
    key = (base_url, api_key)
    if key not in _clients:
        _clients[key] = openai_sdk.AsyncClient(
            api_key=api_key, base_url=base_url, max_retries=0, http_client=http_client(base_url)
        )
    return _clients[key]


def create_stt(s: Settings) -> openai.STT:
    # detect_language: no language is sent, so the GPU server picks Hindi, Gujarati or English
    # for every utterance itself (WHISPER_LANGUAGES on the GPU machine).
    return openai.STT(
        model="whisper-1",
        detect_language=True,
        prompt=build_stt_prompt(s),
        client=_client(s.stt_base_url, s.llm_api_key.get_secret_value()),
        use_realtime=False,
    )


def create_llm(s: Settings) -> openai.LLM:
    return openai.LLM(
        model=s.llm_model,
        client=_client(s.llm_base_url, s.llm_api_key.get_secret_value()),
        temperature=0.0,
        # Lets the model check several days in one turn (e.g. tomorrow, Friday and Saturday).
        parallel_tool_calls=True,
        # llama-server reads max_tokens; strict tool schemas are an OpenAI-only feature.
        extra_body={"max_tokens": s.llm_max_tokens},
        _strict_tool_schema=False,
    )


def create_tts(s: Settings) -> openai.TTS:
    # The server ignores the OpenAI voice name and uses its own Kokoro / Piper voice.
    return openai.TTS(
        model="tts-1",
        voice="alloy",
        client=_client(s.tts_base_url, s.llm_api_key.get_secret_value()),
        response_format="pcm",
        sample_rate=TTS_SAMPLE_RATE,
    )


def create_vad() -> lk_vad.VAD:
    """Silero VAD, loaded once per process (loading takes a moment) and shared by all calls."""
    global _vad
    if _vad is None:
        # 0.4 s of silence ends the caller's turn (LiveKit's default is 0.55 s): every turn
        # answers 0.15 s sooner, and normal breathing pauses are still shorter than that.
        _vad = silero.VAD.load(sample_rate=VAD_SAMPLE_RATE, min_silence_duration=0.4)
    return _vad
