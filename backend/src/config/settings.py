"""Application settings, loaded from environment / .env (12-factor)."""

from __future__ import annotations

from functools import lru_cache
from typing import ClassVar

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config.env_bootstrap import load_env

# Deliberately NOT `env_file=".env"`: that path is resolved against the current
# working directory, so `pytest` from backend/ and `uvicorn` from the repo root
# silently loaded different files (or none). env_bootstrap resolves the .env
# layers from __file__ and pushes them into os.environ instead, which is CWD
# independent and shared with Alembic and the test suite.
load_env()


class Settings(BaseSettings):
    # hide_input_in_errors: a refused boot otherwise echoes the raw input values,
    # secrets included (JWT_SECRET, KAVENEGAR_API_KEY), into the deployment log.
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

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
    # The default is a DEVELOPMENT placeholder and is refused outside development
    # by `_reject_insecure_defaults` below. It is published in this file, so a
    # deployment that silently fell back to it would let anyone who has read the
    # repository mint a token for any user, with any role.
    jwt_secret: str = "change-me-in-production"  # noqa: S105 - dev default only
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60          # 15 minutes
    refresh_token_ttl_seconds: int = 30 * 24 * 3600  # 30 days

    # --- OTP ---
    otp_length: int = 6
    otp_ttl_seconds: int = 120
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    # Same contract as jwt_secret: a published default, refused outside
    # development. A known pepper makes the stored OTP hashes precomputable.
    otp_pepper: str = "change-me-otp-pepper"  # noqa: S105 - dev default only

    # --- OTP delivery provider ---
    # console   -> development/test only; delivers nothing (see ConsoleOtpSender)
    # kavenegar -> Kavenegar Verify Lookup; needs the two KAVENEGAR_* below
    # Any other value refuses to boot. It used to fall back to console silently,
    # which in production meant no code was ever delivered and nothing said so.
    sms_provider: str = "console"
    # SecretStr so the key never appears in a repr, a traceback or a settings dump.
    kavenegar_api_key: SecretStr = SecretStr("")
    # Template name as created in the Kavenegar panel; it holds the message text.
    kavenegar_otp_template: str = ""
    kavenegar_base_url: str = "https://api.kavenegar.com"
    sms_timeout_seconds: float = 5.0

    # --- Selling / Marketplace ---
    # Configurable invariant: at most one ACTIVE offer per seller per variant.
    # True = single-offer mode (default); False = marketplace multi-offer mode.
    selling_one_active_offer_per_variant: bool = True

    # --- Abuse protection (Redis-backed, fail-closed) ---
    # Per-IP ceilings on the unauthenticated auth surface. The per-mobile OTP
    # cooldown already existed and is unchanged; it did nothing against an
    # attacker who simply varies the number, which is how SMS-bombing and SMS
    # cost-inflation attacks work.
    otp_request_ip_limit: int = 10
    otp_request_ip_window_seconds: int = 600
    otp_verify_ip_limit: int = 20
    otp_verify_ip_window_seconds: int = 600
    # Guessing a refresh token is infeasible, so this is a resource-exhaustion
    # ceiling rather than a brute-force one: every attempt costs a DB round trip.
    refresh_ip_limit: int = 30
    refresh_ip_window_seconds: int = 600

    # --- OTP delivery (development only) ---
    # The console sender writes the OTP to the application log. That is a
    # credential in a log file, so it is OFF unless switched on deliberately, and
    # refused outside development entirely (see the validator below). Without
    # this flag the console sender logs only that a code was sent.
    otp_log_codes: bool = False

    # --- Values a production deployment must not be allowed to inherit --------
    # Keyed by attribute name so the message can name the environment variable.
    INSECURE_DEFAULTS: ClassVar[dict[str, str]] = {
        "jwt_secret": "change-me-in-production",
        "otp_pepper": "change-me-otp-pepper",
    }
    #: Environments where the published development defaults are acceptable.
    DEV_ENVIRONMENTS: ClassVar[frozenset[str]] = frozenset({"development", "test", "local"})
    #: OTP delivery providers with a working adapter.
    SMS_PROVIDERS: ClassVar[frozenset[str]] = frozenset({"console", "kavenegar"})

    @property
    def is_development(self) -> bool:
        return self.environment.strip().lower() in self.DEV_ENVIRONMENTS

    @model_validator(mode="after")
    def _validate_sms_provider(self) -> "Settings":
        """Provider misconfiguration fails at boot, in EVERY environment.

        An unknown name or a Kavenegar setup without credentials is never what
        anyone meant, so there is no environment in which booting anyway helps.
        Whether `console` is acceptable depends on the environment and is
        checked with the other production rules in `_reject_insecure_defaults`.
        """
        provider = self.sms_provider.strip().lower()
        if provider not in self.SMS_PROVIDERS:
            raise ValueError(
                f"SMS_PROVIDER={self.sms_provider!r} is not supported; "
                f"use one of {sorted(self.SMS_PROVIDERS)}"
            )
        self.sms_provider = provider
        if provider == "kavenegar":
            missing = [
                name
                for name, value in (
                    ("KAVENEGAR_API_KEY", self.kavenegar_api_key.get_secret_value()),
                    ("KAVENEGAR_OTP_TEMPLATE", self.kavenegar_otp_template),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError(
                    "SMS_PROVIDER=kavenegar requires " + " and ".join(missing)
                )
        if self.sms_timeout_seconds <= 0:
            raise ValueError("SMS_TIMEOUT_SECONDS must be positive")
        return self

    @model_validator(mode="after")
    def _reject_insecure_defaults(self) -> "Settings":
        """Fail to boot rather than run production on a published secret.

        A missing environment variable is the normal way this goes wrong: nothing
        errors, the placeholder is used, and every token in the system becomes
        forgeable by anyone who can read this file. Refusing at import time turns
        a silent, total compromise into a loud deployment failure.

        Length is checked too: HS256 with a short key is brute-forceable offline
        from a single captured token, and RFC 7518 §3.2 requires at least as many
        bits of key as the hash outputs.
        """
        if self.is_development:
            return self

        problems = [
            f"{name.upper()} is still the published development default"
            for name, default in self.INSECURE_DEFAULTS.items()
            if getattr(self, name) == default
        ]
        if len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET is shorter than the 32 bytes HS256 requires")
        if len(self.otp_pepper) < 16:
            problems.append("OTP_PEPPER is shorter than 16 characters")
        if self.otp_log_codes:
            problems.append("OTP_LOG_CODES is enabled, which writes OTP codes to the log")
        if self.sms_provider == "console":
            problems.append(
                "SMS_PROVIDER is 'console', which delivers no OTP; configure a real provider"
            )
        if self.debug:
            problems.append("DEBUG is enabled")

        if problems:
            raise ValueError(
                f"refusing to start in environment={self.environment!r}: "
                + "; ".join(problems)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
