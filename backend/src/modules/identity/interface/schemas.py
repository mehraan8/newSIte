"""Request/response models for the auth API (the HTTP contract)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class OtpRequestIn(BaseModel):
    mobile: str = Field(..., examples=["09123456789", "+989123456789"])


class OtpRequestOut(BaseModel):
    masked_mobile: str
    ttl_seconds: int
    resend_after_seconds: int


class OtpVerifyIn(BaseModel):
    mobile: str = Field(..., examples=["09123456789"])
    code: str = Field(..., min_length=4, max_length=8, examples=["123456"])


class TokensOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AuthOut(BaseModel):
    user_id: str
    mobile: str
    is_new_user: bool
    tokens: TokensOut


class RefreshIn(BaseModel):
    refresh_token: str


class MeOut(BaseModel):
    user_id: str
    mobile: str
    full_name: Optional[str] = None
    status: str
