"""Pipecat services pointed at the GPU model server (OpenAI-compatible HTTP).

STT, LLM and TTS all go through the same server, so one key (LLM_API_KEY) is used.
"""

from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService

from app.core.config import Settings
from app.pipeline.prompts import build_stt_prompt


def create_stt(s: Settings) -> OpenAISTTService:
    return OpenAISTTService(
        api_key=s.llm_api_key.get_secret_value(),
        base_url=s.stt_base_url,
        settings=OpenAISTTService.Settings(model="whisper-1", prompt=build_stt_prompt(s)),
    )


def create_llm(s: Settings) -> OpenAILLMService:
    return OpenAILLMService(
        api_key=s.llm_api_key.get_secret_value(),
        base_url=s.llm_base_url,
        settings=OpenAILLMService.Settings(
            model=s.llm_model, temperature=0.0, max_tokens=s.llm_max_tokens
        ),
    )


def create_tts(s: Settings) -> OpenAITTSService:
    # The server ignores the OpenAI voice name and uses its own Piper voice.
    return OpenAITTSService(
        api_key=s.llm_api_key.get_secret_value(),
        base_url=s.tts_base_url,
        settings=OpenAITTSService.Settings(model="tts-1", voice="alloy"),
    )
