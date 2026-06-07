"""Application settings, loaded from environment / .env (12-factor)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "Persian RTL E-Commerce API"
    environment: str = Field(default="development")
    debug: bool = True
    default_locale: str = "fa-IR"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- Database (async driver for the app; Alembic uses the sync psycopg URL) ---
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/ecommerce"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth / JWT ---
    jwt_secret: str = "change-me-in-production"  # noqa: S105 - dev default only
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60          # 15 minutes
    refresh_token_ttl_seconds: int = 30 * 24 * 3600  # 30 days

    # --- OTP ---
    otp_length: int = 6
    otp_ttl_seconds: int = 120
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_pepper: str = "change-me-otp-pepper"  # noqa: S105 - dev default only
    sms_provider: str = "console"  # console | kavenegar | smsir


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
