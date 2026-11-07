"""Application settings loaded from environment variables and .env."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Secrets use SecretStr so they never print in logs."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    redis_url: str = "redis://localhost:6379/0"

    telnyx_api_key: SecretStr = SecretStr("")
    telnyx_public_key: str = ""
    telnyx_connection_id: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    # Off by default: calls only hear the greeting. Turn on to open the audio stream.
    twilio_stream_enabled: bool = False
    # Hard limit per call so a stuck call cannot burn the Twilio balance.
    max_call_seconds: int = 120

    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    stt_base_url: str = "http://localhost:8002/v1"
    tts_base_url: str = "http://localhost:8003/v1"

    gateway_api_key: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
