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

    # Which carrier is active. Only that carrier's webhook and WebSocket routes answer.
    call_provider: Literal["twilio", "exotel"] = "exotel"

    # Exotel: Settings > API Settings in the Exotel dashboard.
    exotel_api_key: SecretStr = SecretStr("")
    exotel_api_token: SecretStr = SecretStr("")
    exotel_account_sid: str = ""
    exotel_phone_number: str = ""  # your ExoPhone
    # Secret put in the Passthru URL as ?token=... (Exotel documents no webhook signature).
    exotel_webhook_secret: SecretStr = SecretStr("")
    # Optional comma-separated allowlist of Exotel source IPs (ask Exotel support for them).
    exotel_allowed_ips: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    # Off by default: calls only hear the greeting. Turn on to open the audio stream.
    twilio_stream_enabled: bool = False
    # Hard limit per call so a stuck call cannot burn the Twilio balance.
    max_call_seconds: int = 120

    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "qwen"  # one key (LLM_API_KEY) is used for the LLM, STT and TTS servers
    llm_max_tokens: int = 150
    stt_base_url: str = "http://localhost:8002/v1"
    tts_base_url: str = "http://localhost:8003/v1"

    gateway_api_key: SecretStr = SecretStr("")

    # Staff dashboard at /dashboard. Empty password = the dashboard is switched off (404).
    dashboard_password: SecretStr = SecretStr("")
    dashboard_session_hours: int = 12

    # Voice agent. False = old echo test (caller hears their own voice).
    voice_agent_enabled: bool = True
    agent_name: str = "Kodera"
    agent_greeting: str = "Hello, thank you for calling {clinic}. How can I help you today?"
    # --- Where appointments live: "local" (our own table) or "calcom" (self-hosted Cal.diy). ---
    booking_backend: Literal["local", "calcom"] = "local"
    cal_api_url: str = "http://calcom-api:80"  # Cal.diy API v2, reached inside the Docker network
    cal_api_key: SecretStr = SecretStr("")  # Cal.diy: Settings > Developer > API keys (cal_...)
    cal_event_type_id: int = 0  # the "Consultation" event type in Cal.diy
    cal_webhook_secret: SecretStr = SecretStr("")  # same secret you give the Cal.diy webhook
    cal_public_url: str = ""  # e.g. https://cal.example.com (shown as a link on the dashboard)
    cal_booking_page: str = ""  # e.g. dr-sharma/consultation: the patients' booking page path
    clinic_timezone_name: str = "Asia/Kolkata"  # IANA name; Cal.diy needs it, not just an offset
    clinic_phone_country_code: str = "+91"  # callers' numbers are sent to Cal.diy in E.164

    # --- Clinic (the agent is the clinic's phone receptionist). EDIT THESE in .env. ---
    clinic_name: str = "City Care Clinic"
    clinic_doctor: str = "Dr. Sharma"
    clinic_address: str = "12 Main Road, near the bus stand"
    clinic_fees: str = "500 rupees for a consultation"
    # Weekdays the clinic is open: 0 = Monday ... 6 = Sunday.
    clinic_open_days: str = "0,1,2,3,4,5"
    # Consultation sessions, 24-hour HH:MM-HH:MM, comma separated.
    clinic_sessions: str = "10:00-13:00,17:00-20:00"
    clinic_slot_minutes: int = 15
    # Minutes ahead of UTC for the clinic's clock (India = 330).
    clinic_utc_offset_minutes: int = 330
    # After the caller pauses, how long to wait for them to continue before answering.
    # Speech-to-text takes longer than this anyway, so a small value costs no extra delay.
    agent_turn_wait_seconds: float = 0.25
    # Hang up if nobody speaks for this long.
    agent_idle_seconds: int = 45

    @property
    def exotel_allowed_ip_list(self) -> list[str]:
        return [ip.strip() for ip in self.exotel_allowed_ips.split(",") if ip.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
