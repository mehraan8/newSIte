"""Application settings, loaded from environment / .env (12-factor)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config.env_bootstrap import load_env

# Deliberately NOT `env_file=".env"`: that path is resolved against the current
# working directory, so `pytest` from backend/ and `uvicorn` from the repo root
# silently loaded different files (or none). env_bootstrap resolves the .env
# layers from __file__ and pushes them into os.environ instead, which is CWD
# independent and shared with Alembic and the test suite.
load_env()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # --- App ---
    app_name: str = "Persian RTL E-Commerce API"
    environment: str = Field(default="development")
    debug: bool = True
    default_locale: str = "fa-IR"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- Database (async driver for the app; Alembic uses the sync psycopg URL) ---
    # No hardcoded fallback DSN on purpose. env_bootstrap derives this from the
    # POSTGRES_* variables in the root .env; an empty value here means those are
    # missing, and failing loudly beats silently dialling a guessed password.
    database_url: str = ""
    # Scratch database for tests/integration. Derived as <POSTGRES_DB>_test.
    # NEVER point this at a database you care about — the suite TRUNCATEs it.
    test_database_url: str = ""
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

    # --- Selling / Marketplace ---
    # Configurable invariant: at most one ACTIVE offer per seller per variant.
    # True = single-offer mode (default); False = marketplace multi-offer mode.
    selling_one_active_offer_per_variant: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
