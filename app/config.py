"""
Central configuration. All other modules import `settings` from here.
Values are read from environment / .env via pydantic-settings.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Binance ---
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_signal_chat_id: str = ""
    telegram_admin_ids: str = ""

    # --- Database ---
    postgres_user: str = "signals"
    postgres_password: str = "signals"
    postgres_db: str = "signals"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # --- Redis ---
    redis_host: str = "redis"
    redis_port: int = 6379

    # --- Scanner ---
    universe_refresh_sec: int = 900
    scan_interval_sec: int = 30
    scan_timeframes: str = "5m,15m,1h,4h"
    max_symbols: int = 0
    min_quote_volume_usdt: float = 5_000_000

    # --- Signal engine ---
    min_confidence: float = 72.0
    signal_cooldown_sec: int = 3600
    symbol_cooldown_minutes: int = 180
    anti_duplicate_signal: bool = True
    max_signals_per_hour: int = 12
    min_rr: float = 1.8

    # --- Dashboard ---
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    dashboard_secret: str = "change_me"

    # --- Logging ---
    log_level: str = "INFO"
    log_dir: str = "/app/logs"

    timezone: str = "UTC"

    # ---------- helpers ----------
    @property
    def admin_ids(self) -> List[int]:
        if not self.telegram_admin_ids:
            return []
        out: List[int] = []
        for chunk in self.telegram_admin_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit() or (chunk.startswith("-") and chunk[1:].isdigit()):
                out.append(int(chunk))
        return out

    @property
    def timeframes(self) -> List[str]:
        return [tf.strip() for tf in self.scan_timeframes.split(",") if tf.strip()]

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @field_validator("min_confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not (0 <= v <= 100):
            raise ValueError("min_confidence must be 0..100")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
