"""DI wiring for the Identity context. Concrete adapters are assembled here and
injected into use cases — the composition root for this module.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.identity.application.use_cases import (
    Logout,
    RefreshAccessToken,
    RequestOtp,
    VerifyOtp,
)
from src.modules.identity.infrastructure.jwt_service import JwtTokenService
from src.modules.identity.infrastructure.otp_store import RedisOtpStore
from src.modules.identity.domain.entities import may_authenticate
from src.modules.identity.infrastructure.repository import (
    SqlRefreshTokenStore,
    SqlUserStatusQuery,
)
from src.modules.identity.infrastructure.role_repository import SqlPlatformRoleQuery
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
        # Refresh is the one place a blocked account could otherwise renew itself
        # indefinitely, so the account gate applies here too, not only on
        # authenticated requests.
        user_status=SqlUserStatusQuery(session),
    )


def get_logout(session: AsyncSession = Depends(get_session)) -> Logout:
    return Logout(
        tokens=JwtTokenService(),
        refresh_store=SqlRefreshTokenStore(session),
    )


async def get_access_claims(
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> AccessTokenClaims:
    """THE authentication gate. Validates the credential AND the account.

    The single validation path: `get_current_user` and Principal assembly both
    read from here, so authentication cannot drift between them — and so the
    account-status check below cannot be forgotten by a route or a context.

    Two separate questions, in order:

      1. is the CREDENTIAL good?  — signature, algorithm, type, expiry, claim
         shape. Pure JWT work, done by `decode_access`.
      2. may the ACCOUNT act?     — the authoritative answer lives in
         `users.status`, never in the token.

    Why (2) cannot be answered from the token: a JWT is a snapshot of what was
    true when it was signed. Blocking a user is a decision made *after* that, and
    an unexpired signature must not outvote it. ADR-0011 §8 accepts stale ROLES
    for the life of an access token, bounded and deliberate; it does not accept a
    stale ACCOUNT. Shortening the TTL would only narrow the window, not close it.

    The cost is one primary-key read per authenticated request, which is the
    smallest mechanism that makes the guarantee real. It happens HERE, before
    Principal assembly, so a blocked caller never reaches membership resolution,
    the Redis cache, or any authorization decision.

    401, not 403: a blocked account is a failure to authenticate, not a missing
    permission. The message differs from the generic one because reaching it
    requires already holding a server-issued token for that exact subject — it
    discloses nothing the caller does not already have, and a locked-out user
    deserves to know why.
    """
    if not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("توکن دسترسی ارسال نشده است.")
    token = authorization.split(" ", 1)[1].strip()
    claims = JwtTokenService().decode_access(token)

    status = await SqlUserStatusQuery(session).status_of(claims.subject_id)
    if not may_authenticate(status):
        raise UnauthorizedError("حساب کاربری شما غیرفعال است.")
    return claims


# Principal assembly is deliberately NOT wired here. It needs Merchant's
# membership resolution as well as Identity's role grants, and Identity must not
# import Merchant (ADR-0011 §4). The wiring lives in the composition root:
# `src.api.dependencies.get_principal` / `get_principal_factory`, which consumes
# `get_access_claims` above.


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
