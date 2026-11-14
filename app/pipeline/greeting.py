"""Phrases the agent says in every call are made once and kept in memory.

The greeting and the "one moment" filler never change, so their audio is fetched from the voice
server once. That removes a round trip to the GPU server from the start of every call and from
every moment the caller waits for a schedule lookup (the filler used to cost 1 to 1.7 seconds
before it was heard). Until the audio is ready, or if the GPU server is down at start-up, the
call falls back to normal text-to-speech.
"""

import asyncio

import httpx
import structlog

from app.core.config import Settings
from app.pipeline.prompts import build_greeting

log = structlog.get_logger(__name__)

FILLER = "One moment, let me check."
SAMPLE_RATE = 24_000  # what the TTS server returns for "pcm"
RETRY_SECONDS = 30
_cache: dict[str, bytes] = {}


def cached_text(text: str) -> bytes | None:
    return _cache.get(text)


def cached(settings: Settings) -> bytes | None:
    return _cache.get(build_greeting(settings))


async def _fetch(client: httpx.AsyncClient, settings: Settings, text: str) -> bytes | None:
    key = settings.llm_api_key.get_secret_value()
    resp = await client.post(
        f"{settings.tts_base_url.rstrip('/')}/audio/speech",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "tts-1", "voice": "alloy", "input": text, "response_format": "pcm"},
    )
    if resp.status_code == 200 and len(resp.content) > 1000:
        return resp.content
    log.warning("phrase_audio_failed", status=resp.status_code, text=text[:30])
    return None


async def prefetch(settings: Settings) -> None:
    """Fetch the audio of the fixed phrases; keep trying until the GPU server answers."""
    texts = [build_greeting(settings), FILLER]
    while any(t not in _cache for t in texts):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                for text in texts:
                    if text not in _cache and (audio := await _fetch(client, settings, text)):
                        _cache[text] = audio
                        log.info("phrase_audio_cached", text=text[:30], bytes=len(audio))
        except httpx.HTTPError as exc:
            log.warning("phrase_audio_unreachable", error=type(exc).__name__)
        if all(t in _cache for t in texts):
            return
        await asyncio.sleep(RETRY_SECONDS)
