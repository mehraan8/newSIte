"""DI wiring for the Identity context. Concrete adapters are assembled here and
injected into use cases — the composition root for this module.
"""

from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.identity.application.use_cases import (
    RefreshAccessToken,
    RequestOtp,
    VerifyOtp,
)
from src.modules.identity.infrastructure.jwt_service import JwtTokenService
from src.modules.identity.infrastructure.otp_store import RedisOtpStore
from src.modules.identity.infrastructure.repository import (
    SqlRefreshTokenStore,
    SqlUserRepository,
)
from src.modules.identity.infrastructure.sms_sender import (
    ConsoleOtpSender,
    KavenegarOtpSender,
)
from src.config.settings import settings
from src.shared_kernel.application.errors import UnauthorizedError
from src.shared_kernel.infrastructure.database import get_session
from src.shared_kernel.infrastructure.redis_client import get_redis


def _sender():
    if settings.sms_provider == "kavenegar":
        return KavenegarOtpSender(api_key="", template="otp")  # configure via settings
    return ConsoleOtpSender()


def get_request_otp() -> RequestOtp:
    return RequestOtp(otp_store=RedisOtpStore(get_redis()), sender=_sender())


def get_verify_otp(session: AsyncSession = Depends(get_session)) -> VerifyOtp:
    return VerifyOtp(
        otp_store=RedisOtpStore(get_redis()),
        users=SqlUserRepository(session),
        tokens=JwtTokenService(),
        refresh_store=SqlRefreshTokenStore(session),
    )


def get_refresh_token(session: AsyncSession = Depends(get_session)) -> RefreshAccessToken:
    return RefreshAccessToken(
        tokens=JwtTokenService(),
        refresh_store=SqlRefreshTokenStore(session),
    )


class CurrentUser:
    def __init__(self, user_id: uuid.UUID, roles: list[str]) -> None:
        self.id = user_id
        self.roles = roles


async def get_current_user(authorization: str = Header(default="")) -> CurrentUser:
    """Validates the bearer access token. Used to guard protected endpoints."""
    if not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("توکن دسترسی ارسال نشده است.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = JwtTokenService().decode(token)
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("توکن منقضی شده است.") from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("توکن نامعتبر است.") from exc
    if payload.get("type") != "access":
        raise UnauthorizedError("نوع توکن نامعتبر است.")
    return CurrentUser(uuid.UUID(payload["sub"]), payload.get("roles", []))
