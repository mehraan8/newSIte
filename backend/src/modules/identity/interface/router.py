"""Auth HTTP endpoints (the Identity public interface)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.identity.application.use_cases import (
    RefreshAccessToken,
    RequestOtp,
    VerifyOtp,
)
from src.modules.identity.infrastructure.repository import SqlUserRepository
from src.modules.identity.interface.dependencies import (
    CurrentUser,
    get_current_user,
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


@router.post("/otp/request", response_model=OtpRequestOut, status_code=status.HTTP_201_CREATED)
async def request_otp(body: OtpRequestIn, uc: RequestOtp = Depends(get_request_otp)) -> OtpRequestOut:
    result = await uc.execute(body.mobile)
    return OtpRequestOut(**result.__dict__)


@router.post("/otp/verify", response_model=AuthOut)
async def verify_otp(body: OtpVerifyIn, uc: VerifyOtp = Depends(get_verify_otp)) -> AuthOut:
    result = await uc.execute(body.mobile, body.code)
    return AuthOut(
        user_id=result.user_id,
        mobile=result.mobile,
        is_new_user=result.is_new_user,
        tokens=TokensOut(**result.tokens.__dict__),
    )


@router.post("/token/refresh", response_model=TokensOut)
async def refresh_token(body: RefreshIn, uc: RefreshAccessToken = Depends(get_refresh_token)) -> TokensOut:
    tokens = await uc.execute(body.refresh_token)
    return TokensOut(**tokens.__dict__)


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
