"""Ports (abstractions) the Identity use cases depend on.

Infrastructure provides concrete adapters. The application layer never imports
SQLAlchemy, Redis, or an SMS SDK directly — only these interfaces.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from src.modules.identity.domain.entities import User
from src.shared_kernel.domain.value_objects import Mobile


class UserRepository(Protocol):
    async def get_by_mobile(self, mobile: Mobile) -> User | None: ...
    async def get_by_id(self, user_id: uuid.UUID) -> User | None: ...
    async def add(self, user: User) -> None: ...
    async def update(self, user: User) -> None: ...


class OtpChallenge:
    """Lightweight DTO for what the OTP store holds."""

    def __init__(self, code_hash: str, attempts: int, expires_in: int) -> None:
        self.code_hash = code_hash
        self.attempts = attempts
        self.expires_in = expires_in


class OtpStore(Protocol):
    async def save(self, mobile: Mobile, code_hash: str, ttl: int) -> None: ...
    async def get(self, mobile: Mobile) -> OtpChallenge | None: ...
    async def increment_attempts(self, mobile: Mobile) -> int: ...
    async def delete(self, mobile: Mobile) -> None: ...
    async def seconds_until_resend(self, mobile: Mobile) -> int: ...
    async def mark_sent(self, mobile: Mobile, cooldown: int) -> None: ...


class OtpSender(Protocol):
    async def send_code(self, mobile: Mobile, code: str) -> None: ...


@dataclass(frozen=True)
class AccessTokenClaims:
    """A validated access token, reduced to the claims ADR-0011 §8 allows.

    Produced only by `TokenService.decode_access`, which rejects anything that
    fails validation — so holding one of these means signature, expiry, type,
    subject, tenant claim and roles SHAPE have all already passed.

    `roles` are role CODES, not permissions: resolving them is the composition
    root's job via RoleGrantsQuery. `tenant_id` is Optional because the system is
    multi-tenant READY, not yet multi-tenant — there is no tenants table and no
    user.tenant_id column, so today the claim is present and null. The claim's
    PRESENCE is still mandatory (ADR-0011 §8), so a token minted before tenant
    scoping existed cannot silently pass once it does.
    """

    subject_id: uuid.UUID
    tenant_id: uuid.UUID | None
    role_codes: tuple[str, ...]


class TokenService(Protocol):
    def create_access_token(
        self, user_id: uuid.UUID, roles: list[str], tenant_id: uuid.UUID | None = None
    ) -> str: ...
    def create_refresh_token(self, user_id: uuid.UUID, family_id: uuid.UUID) -> tuple[str, str]:
        """Returns (raw_token, token_hash)."""
        ...
    def decode(self, token: str) -> dict: ...
    def decode_access(self, token: str) -> AccessTokenClaims:
        """Decode AND validate an access token. Raises UnauthorizedError on any
        failure — never returns partially-trusted claims."""
        ...
    def hash_token(self, raw_token: str) -> str: ...


class PlatformRoleQuery(Protocol):
    """A user's platform role CODES, from Identity's `user_roles` grants.

    Identity owns platform roles (ADR-0011 §4). This is read at token ISSUANCE
    only — never per request. ADR-0011 §8 puts roles in the token and accepts the
    resulting staleness, bounded by the access-token lifetime; re-reading them on
    every request is the rejected architecture on the archaeology branch.
    """

    async def role_codes_for_user(self, user_id: uuid.UUID) -> tuple[str, ...]: ...


class RefreshTokenStore(Protocol):
    async def save(
        self, user_id: uuid.UUID, token_hash: str, family_id: uuid.UUID, ttl: int
    ) -> None: ...
    async def is_valid(self, token_hash: str) -> bool: ...
    async def rotate(self, old_hash: str, new_hash: str, ttl: int) -> None: ...
    async def revoke_family(self, family_id: uuid.UUID) -> None: ...
    async def get_meta(self, token_hash: str) -> dict | None: ...
