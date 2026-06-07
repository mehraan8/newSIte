"""Application DTOs for the Identity use cases (framework-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OtpRequested:
    masked_mobile: str
    ttl_seconds: int
    resend_after_seconds: int


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


@dataclass(frozen=True)
class AuthResult:
    user_id: str
    mobile: str
    is_new_user: bool
    tokens: AuthTokens
