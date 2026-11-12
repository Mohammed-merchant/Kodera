"""Measures how long the caller waits, per turn, and where the time goes.

response_ms  caller stopped talking -> agent's first sound
stt_ms / llm_ms / tts_ms  time to first byte of each step (from Pipecat's metrics)
"""

import time

from pipecat.frames.frames import BotStartedSpeakingFrame, MetricsFrame, VADUserStoppedSpeakingFrame
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed


class LatencyObserver(BaseObserver):
    def __init__(self) -> None:
        super().__init__(observe_every_push=False)  # see each frame once
        self._stopped_at: float | None = None
        self._response_ms: int | None = None
        self._ttfb: dict[str, int] = {}

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._stopped_at = time.monotonic()
            self._response_ms, self._ttfb = None, {}
        elif isinstance(frame, MetricsFrame):
            for item in frame.data:
                if not isinstance(item, TTFBMetricsData):
                    continue
                name = item.processor.upper()
                step = (
                    "stt"
                    if "STT" in name
                    else "llm"
                    if "LLM" in name
                    else "tts"
                    if "TTS" in name
                    else ""
                )
                if step and step not in self._ttfb:  # the first one is what the caller waits for
                    self._ttfb[step] = round(item.value * 1000)
        elif (
            isinstance(frame, BotStartedSpeakingFrame)
            and self._stopped_at is not None
            and self._response_ms is None
        ):
            self._response_ms = round((time.monotonic() - self._stopped_at) * 1000)

    def take(self) -> dict[str, int | None]:
        """Timings for the agent turn that just ended (each turn is reported once)."""
        out = {
            "response_ms": self._response_ms,
            "stt_ms": self._ttfb.get("stt"),
            "llm_ms": self._ttfb.get("llm"),
            "tts_ms": self._ttfb.get("tts"),
        }
        self._stopped_at, self._response_ms, self._ttfb = None, None, {}
        return out
