"""Identity use cases — the OTP login flow.

Each use case is a single-responsibility object: it orchestrates domain objects
and ports, enforces application rules (rate limiting, attempt caps), and returns
a DTO. No framework or infrastructure types leak in here.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from src.config.settings import settings
from src.modules.identity.application.dtos import (
    AuthResult,
    AuthTokens,
    OtpRequested,
)
from src.modules.identity.application.ports import (
    OtpSender,
    OtpStore,
    RefreshTokenStore,
    TokenService,
    UserRepository,
)
from src.modules.identity.domain.entities import User
from src.modules.identity.domain.exceptions import UserBlockedError
from src.shared_kernel.application.errors import (
    ForbiddenError,
    RateLimitedError,
    UnauthorizedError,
)
from src.shared_kernel.domain.base import new_uuid
from src.shared_kernel.domain.value_objects import Mobile


def _hash_code(code: str) -> str:
    """Peppered SHA-256. OTP codes are never stored in plaintext."""
    return hashlib.sha256((settings.otp_pepper + code).encode()).hexdigest()


def _generate_code(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


class RequestOtp:
    def __init__(self, otp_store: OtpStore, sender: OtpSender) -> None:
        self._store = otp_store
        self._sender = sender

    async def execute(self, raw_mobile: str) -> OtpRequested:
        mobile = Mobile.parse(raw_mobile)

        cooldown = await self._store.seconds_until_resend(mobile)
        if cooldown > 0:
            raise RateLimitedError(
                f"لطفاً {cooldown} ثانیه دیگر برای دریافت کد تلاش کنید."
            )

        code = _generate_code(settings.otp_length)
        await self._store.save(mobile, _hash_code(code), settings.otp_ttl_seconds)
        await self._store.mark_sent(mobile, settings.otp_resend_cooldown_seconds)
        await self._sender.send_code(mobile, code)

        return OtpRequested(
            masked_mobile=mobile.masked,
            ttl_seconds=settings.otp_ttl_seconds,
            resend_after_seconds=settings.otp_resend_cooldown_seconds,
        )


class VerifyOtp:
    def __init__(
        self,
        otp_store: OtpStore,
        users: UserRepository,
        tokens: TokenService,
        refresh_store: RefreshTokenStore,
    ) -> None:
        self._store = otp_store
        self._users = users
        self._tokens = tokens
        self._refresh = refresh_store

    async def execute(self, raw_mobile: str, code: str) -> AuthResult:
        mobile = Mobile.parse(raw_mobile)
        challenge = await self._store.get(mobile)
        if challenge is None:
            raise UnauthorizedError("کد منقضی شده یا یافت نشد. دوباره درخواست دهید.")

        attempts = await self._store.increment_attempts(mobile)
        if attempts > settings.otp_max_attempts:
            await self._store.delete(mobile)
            raise UnauthorizedError("تعداد تلاش‌های مجاز به پایان رسید. کد جدید بگیرید.")

        # Constant-time comparison defeats timing attacks.
        if not hmac.compare_digest(challenge.code_hash, _hash_code(code)):
            raise UnauthorizedError("کد وارد شده صحیح نیست.")

        await self._store.delete(mobile)  # single-use

        user = await self._users.get_by_mobile(mobile)
        is_new = user is None
        if user is None:
            user = User.register(mobile)
            await self._users.add(user)
        # Domain raises a pure domain error; translate it to a transport-mappable
        # application error here, at the boundary (domain stays HTTP-agnostic).
        try:
            user.mark_logged_in()
        except UserBlockedError as exc:
            raise ForbiddenError(exc.message_fa) from exc
        await self._users.update(user)

        tokens = await self._issue_tokens(user)
        return AuthResult(
            user_id=str(user.id),
            mobile=str(user.mobile),
            is_new_user=is_new,
            tokens=tokens,
        )

    async def _issue_tokens(self, user: User) -> AuthTokens:
        family_id = new_uuid()
        access = self._tokens.create_access_token(user.id, roles=[])
        raw_refresh, refresh_hash = self._tokens.create_refresh_token(user.id, family_id)
        await self._refresh.save(
            user.id, refresh_hash, family_id, settings.refresh_token_ttl_seconds
        )
        return AuthTokens(access_token=access, refresh_token=raw_refresh)


class RefreshAccessToken:
    """Rotating refresh tokens with reuse detection."""

    def __init__(self, tokens: TokenService, refresh_store: RefreshTokenStore) -> None:
        self._tokens = tokens
        self._refresh = refresh_store

    async def execute(self, raw_refresh: str) -> AuthTokens:
        try:
            payload = self._tokens.decode(raw_refresh)
        except Exception as exc:  # noqa: BLE001
            raise UnauthorizedError("توکن نامعتبر است.") from exc

        if payload.get("type") != "refresh":
            raise UnauthorizedError("نوع توکن نامعتبر است.")

        token_hash = self._tokens.hash_token(raw_refresh)
        meta = await self._refresh.get_meta(token_hash)

        # Reuse detection: a presented-but-unknown refresh token means the
        # family may be compromised → revoke the whole family.
        if meta is None:
            import uuid as _uuid

            family = payload.get("family_id")
            if family:
                await self._refresh.revoke_family(_uuid.UUID(family))
            raise UnauthorizedError("توکن نامعتبر یا استفاده‌شده است.")

        import uuid as _uuid

        user_id = _uuid.UUID(payload["sub"])
        family_id = _uuid.UUID(payload["family_id"])

        access = self._tokens.create_access_token(user_id, roles=[])
        new_raw, new_hash = self._tokens.create_refresh_token(user_id, family_id)
        await self._refresh.rotate(token_hash, new_hash, settings.refresh_token_ttl_seconds)
        return AuthTokens(access_token=access, refresh_token=new_raw)
