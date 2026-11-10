"""Voice agent for one phone call.

    Exotel WebSocket -> VAD -> STT -> LLM -> TTS -> Exotel WebSocket

STT, LLM and TTS run on the GPU server (see model_server/). This module only wires them
together with Pipecat; it loads no model itself. Pipecat is imported inside run_call so
the rest of the app (and its tests) do not pay for that import.
"""

import asyncio
from typing import Any

import httpx
import structlog
from fastapi import WebSocket

from app.core.config import Settings
from app.pipeline.prompts import build_greeting, build_system_prompt

log = structlog.get_logger(__name__)

PIPELINE_INPUT_RATE = 16_000  # what VAD and STT work with
PIPELINE_OUTPUT_RATE = 24_000  # what the TTS server returns; the serializer resamples to 8 kHz


async def gpu_server_reachable(settings: Settings) -> bool:
    """Quick check that the GPU server answers, so a dead tunnel ends the call at once
    instead of leaving the caller in silence."""
    root = settings.llm_base_url.removesuffix("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            return (await client.get(f"{root}/health")).status_code == 200
    except httpx.HTTPError:
        return False


def warm_up() -> None:
    """Import Pipecat and load the VAD model once at startup (about 4 s on a small server),
    so the first caller does not hear silence while that happens."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.pipeline.worker import PipelineWorker  # noqa: F401
    from pipecat.serializers.exotel import ExotelFrameSerializer  # noqa: F401
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport  # noqa: F401

    from app.pipeline import services  # noqa: F401

    SileroVADAnalyzer()


async def run_call(ws: WebSocket, *, stream_sid: str, call_sid: str, settings: Settings) -> str:
    """Run the agent until the call ends. Returns a short reason for the call log."""
    if not await gpu_server_reachable(settings):
        log.error("gpu_server_unreachable", call_sid=call_sid, url=settings.llm_base_url)
        return "gpu unavailable"

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.serializers.exotel import ExotelFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from app.pipeline.services import create_llm, create_stt, create_tts

    transport = FastAPIWebsocketTransport(
        ws,
        FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=ExotelFrameSerializer(stream_sid=stream_sid, call_sid=call_sid),
        ),
    )

    context = LLMContext([{"role": "system", "content": build_system_prompt(settings.agent_name)}])
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Default VAD settings: Pipecat's smart end-of-turn model decides when the
            # caller has finished, so we do not add our own silence delay.
            vad_analyzer=SileroVADAnalyzer()
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            create_stt(settings),
            aggregators.user(),
            create_llm(settings),
            create_tts(settings),
            transport.output(),
            aggregators.assistant(),
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=PIPELINE_INPUT_RATE,
            audio_out_sample_rate=PIPELINE_OUTPUT_RATE,
        ),
        idle_timeout_secs=settings.agent_idle_seconds,
    )

    reason = "call ended"

    @transport.event_handler("on_client_connected")  # type: ignore[untyped-decorator]
    async def _greet(_transport: Any, _client: Any) -> None:
        # A fixed greeting skips the LLM, so the caller hears something right away.
        text = build_greeting(settings.agent_greeting, settings.agent_name)
        await worker.queue_frames([TTSSpeakFrame(text)])

    @transport.event_handler("on_client_disconnected")  # type: ignore[untyped-decorator]
    async def _hangup(_transport: Any, _client: Any) -> None:
        await worker.cancel()

    try:
        await asyncio.wait_for(
            PipelineRunner(handle_sigint=False).run(worker), settings.max_call_seconds
        )
    except TimeoutError:
        reason = "time limit"
    except Exception:
        log.exception("agent_failed", call_sid=call_sid)
        reason = "agent error"
    return reason
