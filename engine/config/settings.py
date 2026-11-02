"""Environment settings (secrets, endpoints, runtime mode)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(StrEnum):
    REPLAY = "replay"
    LIVE_DATA = "live_data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: AppMode = AppMode.REPLAY
    timezone: str = "Asia/Kolkata"
    config_dir: Path = Path("config")
    log_level: str = "INFO"

    kotak_consumer_key: SecretStr | None = None
    kotak_ucc: str | None = None
    kotak_mobile: str | None = None
    kotak_mpin: SecretStr | None = None
    kotak_totp_secret: SecretStr | None = None
    kotak_env: str = "prod"

    finnhub_api_key: SecretStr | None = None

    llm_provider: str = "anthropic"
    llm_api_key: SecretStr | None = None
    llm_model_fast: str = "claude-haiku-5-5"
    llm_model_smart: str = "claude-sonnet-5-5"
    llm_monthly_budget_usd: float = 40.0

    supabase_url: str | None = None
    supabase_service_key: SecretStr | None = None
    database_url: str = "postgresql+psycopg://papertrade:papertrade@localhost:5432/papertrade"

    storage_endpoint: str | None = None
    storage_bucket: str | None = None
    storage_access_key: SecretStr | None = None
    storage_secret_key: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    def require_live_credentials(self) -> None:
        """In live_data mode the Kotak credentials must be present."""
        if self.app_mode is not AppMode.LIVE_DATA:
            return
        missing = [
            name
            for name in ("kotak_consumer_key", "kotak_ucc", "kotak_mobile", "kotak_mpin", "kotak_totp_secret")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(f"live_data mode needs: {', '.join(m.upper() for m in missing)}")
