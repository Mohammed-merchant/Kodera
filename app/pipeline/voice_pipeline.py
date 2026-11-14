"""Voice agent for one phone call, built on LiveKit Agents.

    Exotel WebSocket -> VAD -> STT -> LLM (+ tools, reply guard) -> TTS -> Exotel WebSocket

STT, LLM and TTS run on the GPU server (see model_server/). This module only wires them
together with a LiveKit AgentSession; it loads no model itself (except the small Silero VAD).
The session reads and writes the Exotel socket directly (app/pipeline/exotel_io.py).
LiveKit is imported inside the functions so the rest of the app (and its tests) do not pay for
that import.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterable, AsyncIterator
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

FRAME_SECONDS = 0.1  # ready-made audio is handed to the session in 100 ms frames


async def gpu_server_reachable(settings: Settings) -> bool:
    """Quick check that the GPU server answers, so a dead tunnel ends the call at once
    instead of leaving the caller in silence."""
    root = settings.llm_base_url.removesuffix("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            return (await client.get(f"{root}/health")).status_code == 200
    except httpx.HTTPError:
        return False


def preload() -> None:
    """The slow part of the warm-up (heavy imports, ~10-20 s on a small server). Safe to run
    in a worker thread, so startup and /health are not delayed."""
    import livekit.agents  # noqa: F401
    import numpy  # noqa: F401
    import onnxruntime  # noqa: F401
    import openai  # noqa: F401

    with contextlib.suppress(ImportError):  # what the OpenAI plugin pulls in on import
        import openai.types.beta.realtime  # noqa: F401
        import openai.types.realtime  # noqa: F401


def warm_up() -> None:
    """Register the LiveKit plugins and load the VAD model once at startup, so the first caller
    does not hear silence while that happens. MUST run on the main thread (LiveKit requires
    plugins to be registered there); after preload() it takes about a second."""
    preload()  # nothing to do if it already ran
    from app.pipeline import exotel_io  # noqa: F401
    from app.pipeline.services import create_vad

    create_vad()


async def _pcm_frames(pcm: bytes, sample_rate: int) -> AsyncIterator[Any]:
    """Ready-made 16-bit mono PCM as LiveKit audio frames."""
    from livekit import rtc

    step = int(sample_rate * FRAME_SECONDS) * 2
    for i in range(0, len(pcm) - len(pcm) % 2, step):
        part = pcm[i : i + step]
        yield rtc.AudioFrame(
            data=part, sample_rate=sample_rate, num_channels=1, samples_per_channel=len(part) // 2
        )


def _make_agent_class() -> type:
    """The Agent subclass is built on first use, so importing this module stays cheap."""
    from livekit.agents import Agent, llm
    from livekit.agents.voice.agent import ModelSettings

    from app.pipeline.guards import ReplyGuard

    class ClinicAgent(Agent):
        def __init__(self, *, recorder: CallRecorder, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._recorder = recorder

        async def llm_node(
            self,
            chat_ctx: llm.ChatContext,
            tools: list[llm.Tool],
            model_settings: ModelSettings,
        ) -> AsyncIterator[Any]:  # ChatChunk | str | FlushSentinel
            """The model's reply, checked sentence by sentence (blocks false "booked" claims
            and medical advice). While a tool runs, the caller hears a short filler."""
            guard = ReplyGuard(self._recorder)
            spoke = False
            filler_said = False
            async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
                if isinstance(chunk, str):
                    for text in await guard.feed(chunk):
                        spoke = True
                        yield text
                    continue
                if not isinstance(chunk, llm.ChatChunk):
                    yield chunk
                    continue
                delta = chunk.delta
                if delta is not None and delta.content:
                    for text in await guard.feed(delta.content):
                        spoke = True
                        yield text
                if delta is not None and delta.tool_calls:
                    for text in await guard.end():
                        spoke = True
                        yield text
                    if not spoke and not filler_said:
                        # Looking up slots takes a moment; fill the silence so the caller
                        # knows we heard. tts_node plays it from ready-made audio.
                        filler_said = True
                        yield greeting_audio.FILLER
                if (delta is not None and (delta.tool_calls or delta.extra)) or chunk.usage:
                    rest = delta.model_copy(update={"content": None}) if delta else None
                    yield chunk.model_copy(update={"delta": rest})
            for text in await guard.end():
                yield text

        async def tts_node(
            self, text: AsyncIterable[str], model_settings: ModelSettings
        ) -> AsyncIterator[Any]:
            """Speech for the reply. The filler phrase plays from ready-made audio (no round
            trip to the voice server); everything else goes to the GPU TTS server."""
            source = aiter(text)
            seen: list[str] = []
            joined = ""
            maybe_filler = True
            async for piece in source:
                seen.append(piece)
                joined += piece
                if not greeting_audio.FILLER.startswith(joined.strip()):
                    maybe_filler = False
                    break
            ready = greeting_audio.cached_text(greeting_audio.FILLER)
            if maybe_filler and joined.strip() == greeting_audio.FILLER and ready:
                async for frame in _pcm_frames(ready, greeting_audio.SAMPLE_RATE):
                    yield frame
                return

            async def replay() -> AsyncIterator[str]:
                for piece in seen:
                    yield piece
                async for piece in source:
                    yield piece

            async for frame in Agent.default.tts_node(self, replay(), model_settings):
                yield frame

    return ClinicAgent


_agent_class: type | None = None


def _agent_cls() -> type:
    global _agent_class
    if _agent_class is None:
        _agent_class = _make_agent_class()
    return _agent_class


def _seconds_to_ms(value: Any) -> int | None:
    return round(value * 1000) if isinstance(value, int | float) else None


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
    # Checked while the session is being built, so it adds no waiting before the greeting.
    gpu_check = asyncio.create_task(gpu_server_reachable(settings))

    from livekit.agents import AgentSession, llm
    from livekit.agents.voice import events

    from app.pipeline.exotel_io import ExotelSocket
    from app.pipeline.services import create_llm, create_stt, create_tts, create_vad
    from app.pipeline.tools import build_tools

    recorder = CallRecorder(redis, call_sid, provider="exotel", caller=caller_number)
    await recorder.start()

    greeting = build_greeting(settings)
    greeting_pcm = greeting_audio.cached(settings)
    tools = build_tools(
        settings, session_factory, caller_number=caller_number, call_sid=call_sid, recorder=recorder
    )
    # The greeting is spoken with say() and kept out of the history there, so it is put into
    # the history here. Without it the model does not know it already greeted, and answers
    # every "hello" with the greeting again.
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="assistant", content=greeting)
    agent = _agent_cls()(
        recorder=recorder,
        instructions=build_system_prompt(settings, schedule_from_settings(settings).now()),
        chat_ctx=chat_ctx,
        tools=tools,
    )
    session: AgentSession[None] = AgentSession(
        stt=create_stt(settings),
        llm=create_llm(settings),
        tts=create_tts(settings),
        vad=create_vad(),
        turn_handling={
            "turn_detection": "vad",
            # After the caller pauses, how long to wait for them to continue before answering.
            "endpointing": {"mode": "fixed", "min_delay": settings.agent_turn_wait_seconds},
            # Plain voice-activity interruptions: the "adaptive" mode needs LiveKit Cloud.
            # (Exotel audio cannot be paused, so a false interruption cannot resume either.)
            "interruption": {"mode": "vad", "resume_false_interruption": False},
        },
        # Hang up if nobody speaks for this long (the session marks the caller "away").
        user_away_timeout=float(settings.agent_idle_seconds),
    )
    phone = ExotelSocket(ws, stream_sid)
    session.input.audio = phone.audio_input
    session.output.audio = phone.audio_output

    ended = asyncio.Event()
    reason = "call ended"
    gpu_down = False
    background: set[asyncio.Task[Any]] = set()
    last_caller_metrics: dict[str, Any] = {}

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    def finish(why: str) -> None:
        nonlocal reason
        if not ended.is_set():
            reason = why
            ended.set()

    @session.on("conversation_item_added")
    def _said(ev: events.ConversationItemAddedEvent) -> None:
        nonlocal last_caller_metrics
        item = ev.item
        if not isinstance(item, llm.ChatMessage):
            return
        text = (item.text_content or "").strip()
        metrics: dict[str, Any] = dict(item.metrics or {})
        if item.role == "user":
            last_caller_metrics = metrics
            spawn(recorder.add_turn("caller", text))
        elif item.role == "assistant":
            # The filler is not a real answer; the greeting is recorded when it is played.
            text = text.removeprefix(greeting_audio.FILLER).strip()
            if not text or text == greeting.strip():
                return
            stopped = last_caller_metrics.get("stopped_speaking_at")
            started = metrics.get("started_speaking_at")
            response = started - stopped if stopped and started and started > stopped else None
            spawn(
                recorder.add_turn(
                    "agent",
                    text,
                    response_ms=_seconds_to_ms(response),
                    stt_ms=_seconds_to_ms(last_caller_metrics.get("transcription_delay")),
                    llm_ms=_seconds_to_ms(metrics.get("llm_node_ttft")),
                    tts_ms=_seconds_to_ms(metrics.get("tts_node_ttfb")),
                )
            )

    @session.on("user_state_changed")
    def _away(ev: events.UserStateChangedEvent) -> None:
        if ev.new_state == "away":
            log.info("caller_silent_hanging_up", call_sid=call_sid)
            finish("idle")

    @session.on("close")
    def _closed(ev: events.CloseEvent) -> None:
        if ev.error is not None:
            log.error("agent_session_error", call_sid=call_sid, error=str(ev.error))
            finish("agent error")
        else:
            finish("call ended")

    await asyncio.wait({gpu_check}, timeout=0.05)  # a refused connection answers within this
    if gpu_check.done() and not gpu_check.result():  # fast failure: do not even start
        log.error("gpu_server_unreachable", call_sid=call_sid, url=settings.llm_base_url)
        await recorder.finish("gpu unavailable")
        await flush_call(redis, session_factory, call_sid)
        return "gpu unavailable"

    async def watch_gpu() -> None:
        nonlocal gpu_down
        if not await gpu_check:  # a slow failure: the session is already running, stop it
            gpu_down = True
            finish("gpu unavailable")

    async def read_phone() -> None:
        finish(await phone.read_until_hangup())

    reader = asyncio.create_task(read_phone())
    watcher = asyncio.create_task(watch_gpu())
    try:
        async with asyncio.timeout(settings.max_call_seconds):
            await session.start(agent, record=False)
            await recorder.add_turn("agent", greeting)
            if greeting_pcm:  # ready-made audio: no round trip to the GPU server
                session.say(
                    greeting,
                    audio=_pcm_frames(greeting_pcm, greeting_audio.SAMPLE_RATE),
                    add_to_chat_ctx=False,
                )
            else:
                session.say(greeting, add_to_chat_ctx=False)
            await ended.wait()
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
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.aclose(), 5)
        await phone.audio_output.aclose()
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
        if background:
            await asyncio.wait(background, timeout=2)
        # Everything recorded in Redis during the call is saved to Postgres now.
        await recorder.finish(reason)
        await flush_call(redis, session_factory, call_sid)
    return reason
