"""Auth HTTP endpoints (the Identity public interface).

These are the only unauthenticated write endpoints in the system, which makes
them the abuse surface. Each one is rate limited per client address before any
work happens — see `_limit`. The per-mobile OTP cooldown that already existed is
unchanged and complementary: it bounds messages to ONE number, this bounds how
many numbers one attacker can hit.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.shared_kernel.infrastructure.rate_limit import RateLimiter, client_identity
from src.shared_kernel.infrastructure.redis_client import get_redis

from src.modules.identity.application.use_cases import (
    Logout,
    RefreshAccessToken,
    RequestOtp,
    VerifyOtp,
)
from src.modules.identity.infrastructure.repository import SqlUserRepository
from src.modules.identity.interface.dependencies import (
    CurrentUser,
    get_current_user,
    get_logout,
    get_refresh_token,
    get_request_otp,
    get_verify_otp,
)
from src.modules.identity.interface.schemas import (
    AuthOut,
    MeOut,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    RefreshIn,
    TokensOut,
)
from src.shared_kernel.application.errors import NotFoundError
from src.shared_kernel.infrastructure.database import get_session

router = APIRouter(prefix="/auth", tags=["auth"])


async def _limit(request: Request, scope: str, limit: int, window: int) -> None:
    """Rate limit this request by client address, refusing on any Redis fault.

    Applied inside the handler rather than as a router-level dependency because
    each endpoint has its own budget: sending an SMS costs real money, verifying
    a code costs a Redis read, and refreshing costs a database round trip.
    """
    await RateLimiter(get_redis()).hit(
        scope, client_identity(request), limit=limit, window_seconds=window
    )


@router.post("/otp/request", response_model=OtpRequestOut, status_code=status.HTTP_201_CREATED)
async def request_otp(
    request: Request, body: OtpRequestIn, uc: RequestOtp = Depends(get_request_otp)
) -> OtpRequestOut:
    """Rate limited per client BEFORE the code is generated or sent.

    The per-mobile cooldown alone left this open: an attacker rotating through
    numbers could send unlimited SMS, which is both a direct billing attack on
    the operator and turns the endpoint into an SMS-bombing relay aimed at
    third parties who never used the site.
    """
    await _limit(request, "otp:request", settings.otp_request_ip_limit,
                 settings.otp_request_ip_window_seconds)
    result = await uc.execute(body.mobile)
    return OtpRequestOut(**result.__dict__)


@router.post("/otp/verify", response_model=AuthOut)
async def verify_otp(
    request: Request, body: OtpVerifyIn, uc: VerifyOtp = Depends(get_verify_otp)
) -> AuthOut:
    """Rate limited per client, on top of the per-challenge attempt cap.

    The attempt cap bounds guesses against ONE challenge; it resets whenever a
    new code is requested. This bounds total guesses from one source, which is
    what an attacker cycling request/verify actually consumes.
    """
    await _limit(request, "otp:verify", settings.otp_verify_ip_limit,
                 settings.otp_verify_ip_window_seconds)
    result = await uc.execute(body.mobile, body.code)
    return AuthOut(
        user_id=result.user_id,
        mobile=result.mobile,
        is_new_user=result.is_new_user,
        tokens=TokensOut(**result.tokens.__dict__),
    )


@router.post("/token/refresh", response_model=TokensOut)
async def refresh_token(
    request: Request, body: RefreshIn, uc: RefreshAccessToken = Depends(get_refresh_token)
) -> TokensOut:
    await _limit(request, "auth:refresh", settings.refresh_ip_limit,
                 settings.refresh_ip_window_seconds)
    tokens = await uc.execute(body.refresh_token)
    return TokensOut(**tokens.__dict__)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshIn, uc: Logout = Depends(get_logout)) -> None:
    """Revokes the presented refresh token's entire family.

    Deliberately not behind `get_current_user`: possession of a valid refresh
    token is the credential, and burning a leaked token must not itself require a
    working access token. The family comes from the token's signed claims, so a
    caller can only ever end their own session.

    204 on success and on repeat — logging out twice is not an error. Already-
    issued access tokens keep working until they expire; nothing here can revoke
    them (see `Logout`).
    """
    await uc.execute(body.refresh_token)


@router.get("/me", response_model=MeOut)
async def me(
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    user = await SqlUserRepository(session).get_by_id(current.id)
    if user is None:
        raise NotFoundError("کاربر یافت نشد.")
    return MeOut(
        user_id=str(user.id),
        mobile=str(user.mobile),
        full_name=user.full_name,
        status=user.status,
    )
