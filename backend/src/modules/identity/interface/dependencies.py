"""DI wiring for the Identity context. Concrete adapters are assembled here and
injected into use cases — the composition root for this module.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.security import PrincipalFactory
from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.identity.application.use_cases import (
    RefreshAccessToken,
    RequestOtp,
    VerifyOtp,
)
from src.modules.identity.infrastructure.jwt_service import JwtTokenService
from src.modules.identity.infrastructure.otp_store import RedisOtpStore
from src.modules.identity.infrastructure.repository import SqlRefreshTokenStore
from src.modules.identity.infrastructure.role_repository import (
    SqlPlatformRoleQuery,
    SqlRoleGrantsQuery,
)
from src.shared_kernel.application.security import Principal
from src.modules.identity.infrastructure.unit_of_work import SqlAlchemyIdentityUnitOfWork
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


def get_verify_otp() -> VerifyOtp:
    # Owns its own transaction via the Identity UoW, so the User aggregate is
    # tracked and its events reach the outbox atomically with the user row.
    # No request session is injected: the UoW opens and commits its own.
    return VerifyOtp(
        otp_store=RedisOtpStore(get_redis()),
        tokens=JwtTokenService(),
        uow_factory=SqlAlchemyIdentityUnitOfWork,
    )


def get_refresh_token(session: AsyncSession = Depends(get_session)) -> RefreshAccessToken:
    return RefreshAccessToken(
        tokens=JwtTokenService(),
        refresh_store=SqlRefreshTokenStore(session),
        # Rotation re-stamps the token with the grants as they are now, which is
        # how a role change takes effect within one access-token lifetime.
        platform_roles=SqlPlatformRoleQuery(session),
    )


def get_principal_factory(
    session: AsyncSession = Depends(get_session),
) -> PrincipalFactory:
    """Composition root for Principal assembly (ADR-0011 §3).

    Wired here because this module is already the Identity composition root and
    owns the RBAC grant adapters. Phase 1F injects the cache-backed
    MembershipQuery; until then memberships resolve to empty, which is
    fail-closed for every seller-scoped question.
    """
    return PrincipalFactory(role_grants=SqlRoleGrantsQuery(session))


async def get_access_claims(authorization: str = Header(default="")) -> AccessTokenClaims:
    """Validates the bearer access token and returns its claims.

    The single validation path: `get_current_user` and Principal assembly both
    read from here, so authentication cannot drift between them.
    """
    if not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("توکن دسترسی ارسال نشده است.")
    token = authorization.split(" ", 1)[1].strip()
    return JwtTokenService().decode_access(token)


async def get_principal(
    claims: AccessTokenClaims = Depends(get_access_claims),
    factory: PrincipalFactory = Depends(get_principal_factory),
) -> Principal:
    """The authenticated caller's capability set.

    Not attached to any route yet — route classification and enforcement are
    Phase 1I/1K. Provided now so Phase 1C onwards has a working seam.
    """
    return await factory.from_claims(claims)


class CurrentUser:
    def __init__(self, user_id: uuid.UUID, roles: list[str]) -> None:
        self.id = user_id
        self.roles = roles


async def get_current_user(
    claims: AccessTokenClaims = Depends(get_access_claims),
) -> CurrentUser:
    """The AUTHENTICATED caller (identity only, no authorization state).

    Retained deliberately: routes that need only "who is this" — checkout, order
    read, /auth/me — should not pay for permission resolution. Authorization
    consumers take `Principal` instead (see `get_principal`).

    Validation now lives in `decode_access`, so this dependency and Principal
    assembly cannot disagree about what a valid token is.
    """
    return CurrentUser(claims.subject_id, list(claims.role_codes))
