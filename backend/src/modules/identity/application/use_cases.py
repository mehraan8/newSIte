"""Identity use cases — the OTP login flow.

Each use case is a single-responsibility object: it orchestrates domain objects
and ports, enforces application rules (rate limiting, attempt caps), and returns
a DTO. No framework or infrastructure types leak in here.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid as _uuid
from typing import Callable

from src.config.settings import settings
from src.modules.identity.application.unit_of_work import IdentityUnitOfWork
from src.modules.identity.application.dtos import (
    AuthResult,
    AuthTokens,
    OtpRequested,
)
from src.modules.identity.application.ports import (
    OtpDeliveryError,
    OtpSender,
    OtpStore,
    PlatformRoleQuery,
    RefreshTokenStore,
    TokenService,
    UserStatusQuery,
)
from src.modules.identity.domain.entities import User, may_authenticate
from src.modules.identity.domain.exceptions import UserBlockedError
from src.shared_kernel.application.errors import (
    ForbiddenError,
    RateLimitedError,
    UnauthorizedError,
)
from src.shared_kernel.domain.base import new_uuid
from src.shared_kernel.domain.value_objects import Mobile

UowFactory = Callable[[], IdentityUnitOfWork]


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
        try:
            await self._sender.send_code(mobile, code)
        except OtpDeliveryError:
            # The user never received this code, so it must not stay verifiable.
            # The cooldown is left in place deliberately: it is what stops a
            # caller from hammering a failing provider, one request per second.
            await self._store.delete(mobile)
            raise

        return OtpRequested(
            masked_mobile=mobile.masked,
            ttl_seconds=settings.otp_ttl_seconds,
            resend_after_seconds=settings.otp_resend_cooldown_seconds,
        )


class VerifyOtp:
    """Runs the login write inside the Identity Unit of Work.

    The UoW is what drains the User aggregate's events (UserRegistered /
    UserLoggedIn) into the outbox in the SAME transaction as the user row. This
    use case previously took a bare repository on the request session, so nothing
    ever collected those events: they were recorded on the aggregate and dropped,
    and the registered notification consumer had no producer.
    """

    def __init__(
        self,
        otp_store: OtpStore,
        tokens: TokenService,
        uow_factory: "UowFactory",
    ) -> None:
        self._store = otp_store
        self._tokens = tokens
        self._uow_factory = uow_factory

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

        uow = self._uow_factory()
        async with uow:
            user = await uow.users.get_by_mobile(mobile)
            is_new = user is None
            if user is None:
                user = User.register(mobile)
                await uow.users.add(user)
            # Domain raises a pure domain error; translate it to a transport-mappable
            # application error here, at the boundary (domain stays HTTP-agnostic).
            try:
                user.mark_logged_in()
            except UserBlockedError as exc:
                raise ForbiddenError(exc.message_fa) from exc
            await uow.users.update(user)

            tokens = await self._issue_tokens(uow, user)
            # One atomic commit: the user row, the refresh token, and the
            # UserRegistered/UserLoggedIn outbox rows land together.
            await uow.commit()

        return AuthResult(
            user_id=str(user.id),
            mobile=str(user.mobile),
            is_new_user=is_new,
            tokens=tokens,
        )

    async def _issue_tokens(self, uow, user: User) -> AuthTokens:
        family_id = new_uuid()
        # Platform roles are stamped into the token here, once, from Identity's
        # own grants (ADR-0011 §8). This was `roles=[]`, which is why nothing in
        # the system could enforce anything: every token claimed no authority.
        role_codes = await uow.platform_roles.role_codes_for_user(user.id)
        access = self._tokens.create_access_token(user.id, roles=list(role_codes))
        raw_refresh, refresh_hash = self._tokens.create_refresh_token(user.id, family_id)
        await uow.refresh_tokens.save(
            user.id, refresh_hash, family_id, settings.refresh_token_ttl_seconds
        )
        return AuthTokens(access_token=access, refresh_token=raw_refresh)


class RefreshAccessToken:
    """Rotating refresh tokens with reuse detection.

    Refresh is also the point at which a role change takes effect: the new access
    token is stamped with the grants as they are NOW. That, plus the access-token
    TTL, is the whole of ADR-0011's bounded platform-role staleness.
    """

    def __init__(
        self,
        tokens: TokenService,
        refresh_store: RefreshTokenStore,
        platform_roles: PlatformRoleQuery,
        user_status: UserStatusQuery,
    ) -> None:
        self._tokens = tokens
        self._refresh = refresh_store
        self._platform_roles = platform_roles
        self._user_status = user_status

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
            family = payload.get("family_id")
            if family:
                await self._refresh.revoke_family(_uuid.UUID(family))
            raise UnauthorizedError("توکن نامعتبر یا استفاده‌شده است.")

        user_id = _uuid.UUID(payload["sub"])
        family_id = _uuid.UUID(payload["family_id"])

        # The account gate, checked AFTER reuse detection so a blocked user
        # replaying a stolen token still burns the family (D1 semantics are
        # unchanged), and BEFORE minting so a blocked account cannot renew.
        # Without this, blocking a user only lasted until their current access
        # token expired: refresh would hand them a fresh one indefinitely, which
        # made account lockout cosmetic.
        if not may_authenticate(await self._user_status.status_of(user_id)):
            raise UnauthorizedError("حساب کاربری شما غیرفعال است.")

        # Mint the candidate refresh token first — it is pure computation, and
        # worthless until the row below persists it — then CLAIM the old one.
        # Rotation is the point of no return, so nothing is handed back before
        # the claim succeeds.
        new_raw, new_hash = self._tokens.create_refresh_token(user_id, family_id)
        if not await self._refresh.rotate(
            token_hash, new_hash, settings.refresh_token_ttl_seconds
        ):
            # Someone else consumed this token between the read above and here.
            # That is indistinguishable from replaying a spent token, so it is
            # treated identically: burn the family. Two clients racing a refresh
            # get logged out, which is the safe direction — the alternative is
            # the race that let a thief and the victim each hold a live token
            # while reuse detection never fired.
            await self._refresh.revoke_family(family_id)
            raise UnauthorizedError("توکن نامعتبر یا استفاده‌شده است.")

        role_codes = await self._platform_roles.role_codes_for_user(user_id)
        access = self._tokens.create_access_token(user_id, roles=list(role_codes))
        return AuthTokens(access_token=access, refresh_token=new_raw)


class Logout:
    """Ends a session by revoking the presented refresh token's whole family.

    Takes the REFRESH token, not the access token, for two reasons. Access tokens
    are stateless and cannot be un-issued, so the family is the only thing there
    is to revoke; and possession of a valid refresh token IS the credential, so
    someone holding a leaked one being able to burn it is a feature. Requiring a
    working access token would mean a user whose session is already half-lost
    could not end it.

    Scope is derived from the CRYPTOGRAPHICALLY VALIDATED token, never from a
    request field: `decode` verifies the signature, so `family_id` is a claim the
    server itself minted for that user. A caller cannot name someone else's
    family, and there is no code path that revokes more than one.

    Idempotent. `revoke_family` touches only rows with `revoked_at IS NULL`, so a
    second logout updates nothing and still succeeds — a client retrying after a
    dropped response must not get an error for a session that is already gone.

    What this does NOT do: it does not invalidate already-issued ACCESS tokens.
    Nothing in this architecture can — they are stateless and unrevocable until
    they expire. Immediate account-level enforcement is a different control, and
    it lives in the authentication gate (`get_access_claims`).
    """

    def __init__(self, tokens: TokenService, refresh_store: RefreshTokenStore) -> None:
        self._tokens = tokens
        self._refresh = refresh_store

    async def execute(self, raw_refresh: str) -> None:
        try:
            payload = self._tokens.decode(raw_refresh)
        except Exception as exc:  # noqa: BLE001
            raise UnauthorizedError("توکن نامعتبر است.") from exc
        if payload.get("type") != "refresh":
            raise UnauthorizedError("نوع توکن نامعتبر است.")

        family = payload.get("family_id")
        if not family:
            raise UnauthorizedError("توکن نامعتبر است.")
        await self._refresh.revoke_family(_uuid.UUID(family))
