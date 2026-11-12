"""The greeting is always the same, so its audio is made once and kept in memory.

That removes a round trip to the GPU server from the start of every call: the caller hears
the greeting about half a second sooner. Until the audio is ready (or if the GPU server is
down at start-up) the call falls back to normal text-to-speech.
"""

import asyncio

import httpx
import structlog

from app.core.config import Settings
from app.pipeline.prompts import build_greeting

log = structlog.get_logger(__name__)

SAMPLE_RATE = 24_000  # what the TTS server returns for "pcm"
RETRY_SECONDS = 30
_cache: dict[str, bytes] = {}


def cached(settings: Settings) -> bytes | None:
    return _cache.get(build_greeting(settings))


async def prefetch(settings: Settings) -> None:
    """Fetch the greeting audio; keep trying until the GPU server answers."""
    text = build_greeting(settings)
    key = settings.llm_api_key.get_secret_value()
    while text not in _cache:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{settings.tts_base_url.rstrip('/')}/audio/speech",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "tts-1",
                        "voice": "alloy",
                        "input": text,
                        "response_format": "pcm",
                    },
                )
            if resp.status_code == 200 and len(resp.content) > 1000:
                _cache[text] = resp.content
                log.info("greeting_audio_cached", bytes=len(resp.content))
                return
            log.warning("greeting_audio_failed", status=resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("greeting_audio_unreachable", error=type(exc).__name__)
        await asyncio.sleep(RETRY_SECONDS)
