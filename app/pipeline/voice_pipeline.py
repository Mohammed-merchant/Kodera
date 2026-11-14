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
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.business.appointments import schedule_from_settings
from app.business.call_flush import flush_call
from app.core.config import Settings
from app.pipeline import greeting as greeting_audio
from app.pipeline.call_recorder import CallRecorder
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


async def run_call(
    ws: WebSocket,
    *,
    stream_sid: str,
    call_sid: str,
    caller_number: str,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
) -> str:
    """Run the agent until the call ends. Returns a short reason for the call log."""
    # Checked while the pipeline is being built, so it adds no waiting before the greeting.
    gpu_check = asyncio.create_task(gpu_server_reachable(settings))

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import TTSAudioRawFrame, TTSSpeakFrame
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
    from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
        SpeechTimeoutUserTurnStopStrategy,
    )
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    from app.pipeline.guards import ReplyGuard
    from app.pipeline.latency import LatencyObserver
    from app.pipeline.services import create_llm, create_stt, create_tts
    from app.pipeline.tools import build_tools

    recorder = CallRecorder(redis, call_sid, provider="exotel", caller=caller_number)
    await recorder.start()

    transport = FastAPIWebsocketTransport(
        ws,
        FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=ExotelFrameSerializer(stream_sid=stream_sid, call_sid=call_sid),
        ),
    )

    greeting = build_greeting(settings)
    greeting_pcm = greeting_audio.cached(settings)
    tools = build_tools(
        settings, session_factory, caller_number=caller_number, call_sid=call_sid, recorder=recorder
    )
    # Pipecat does not put the spoken greeting into the chat history. Without this the model
    # does not know it already greeted, and answers every "hello" with the greeting again.
    context = LLMContext(
        [
            {
                "role": "system",
                "content": build_system_prompt(settings, schedule_from_settings(settings).now()),
            },
            {"role": "assistant", "content": greeting},
        ],
        tools=tools,
    )
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            # The default "smart turn" model sometimes held the answer back for 2-3 seconds
            # after the caller had finished. A short fixed wait is faster and predictable:
            # the turn ends once this wait AND the transcript are both in.
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=settings.agent_turn_wait_seconds
                    )
                ]
            ),
        ),
    )

    llm = create_llm(settings)
    tts = create_tts(settings)
    latency = LatencyObserver()
    pipeline = Pipeline(
        [
            transport.input(),
            create_stt(settings),
            aggregators.user(),
            llm,
            ReplyGuard(recorder),  # blocks false "booked" claims and medical advice
            tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=PIPELINE_INPUT_RATE,
            audio_out_sample_rate=PIPELINE_OUTPUT_RATE,
            enable_metrics=True,  # time-to-first-byte of STT, LLM and TTS for every turn
        ),
        observers=[latency],
        idle_timeout_secs=settings.agent_idle_seconds,
    )

    reason = "call ended"
    gpu_down = False

    @aggregators.user().event_handler("on_user_turn_stopped")  # type: ignore[untyped-decorator]
    async def _caller_said(_aggregator: Any, _strategy: Any, message: Any) -> None:
        await recorder.add_turn("caller", message.content or "")

    @aggregators.assistant().event_handler("on_assistant_turn_stopped")  # type: ignore[untyped-decorator]
    async def _agent_said(_aggregator: Any, message: Any) -> None:
        text = (message.content or "").strip()
        if text and text != greeting.strip():  # the greeting is recorded when it is played
            await recorder.add_turn("agent", text, **latency.take())

    @transport.event_handler("on_client_connected")  # type: ignore[untyped-decorator]
    async def _greet(_transport: Any, _client: Any) -> None:
        await recorder.add_turn("agent", greeting)
        if greeting_pcm:  # ready-made audio: no round trip to the GPU server
            await tts.queue_frame(
                TTSAudioRawFrame(
                    audio=greeting_pcm, sample_rate=greeting_audio.SAMPLE_RATE, num_channels=1
                )
            )
        else:
            await worker.queue_frames([TTSSpeakFrame(greeting)])

    @llm.event_handler("on_function_calls_started")  # type: ignore[untyped-decorator]
    async def _filler(_service: Any, _calls: Any) -> None:
        # Looking up slots takes a moment; fill the silence so the caller knows we heard.
        # The audio is ready-made, so it plays at once (no round trip to the voice server).
        ready = greeting_audio.cached_text(greeting_audio.FILLER)
        if ready:
            await tts.queue_frame(
                TTSAudioRawFrame(
                    audio=ready, sample_rate=greeting_audio.SAMPLE_RATE, num_channels=1
                )
            )
        else:
            await tts.queue_frame(TTSSpeakFrame(greeting_audio.FILLER))

    @transport.event_handler("on_client_disconnected")  # type: ignore[untyped-decorator]
    async def _hangup(_transport: Any, _client: Any) -> None:
        await worker.cancel()

    async def _watch_gpu() -> None:
        nonlocal gpu_down
        if not await gpu_check:  # a slow failure: the pipeline is already running, stop it
            gpu_down = True
            await worker.cancel()

    await asyncio.wait({gpu_check}, timeout=0.05)  # a refused connection answers within this
    if gpu_check.done() and not gpu_check.result():  # fast failure: do not even start
        log.error("gpu_server_unreachable", call_sid=call_sid, url=settings.llm_base_url)
        await recorder.finish("gpu unavailable")
        await flush_call(redis, session_factory, call_sid)
        return "gpu unavailable"

    watcher = asyncio.create_task(_watch_gpu())
    try:
        await asyncio.wait_for(
            PipelineRunner(handle_sigint=False).run(worker), settings.max_call_seconds
        )
    except TimeoutError:
        reason = "time limit"
    except Exception:
        log.exception("agent_failed", call_sid=call_sid)
        reason = "agent error"
    finally:
        watcher.cancel()
        if gpu_down:
            reason = "gpu unavailable"
            log.error("gpu_server_unreachable", call_sid=call_sid, url=settings.llm_base_url)
        # Everything recorded in Redis during the call is saved to Postgres now.
        await recorder.finish(reason)
        await flush_call(redis, session_factory, call_sid)
    return reason
