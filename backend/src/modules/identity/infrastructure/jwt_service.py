"""JWT issuing/verification. Access tokens are short-lived & stateless; refresh
tokens are opaque-ish JWTs whose hash is tracked server-side for rotation.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import jwt

from src.config.settings import settings
from src.modules.identity.application.ports import AccessTokenClaims
from src.shared_kernel.application.errors import UnauthorizedError
from src.shared_kernel.domain.base import utcnow

# ADR-0011 §8: the access token carries these claims and NOTHING else that bears
# on authorization. Permissions, seller ids, memberships and PII are forbidden —
# permissions are resolved server-side from role codes, and seller authority is a
# membership resolved per request (ADR-0011 §10), never a bearer claim.
ACCESS_TOKEN_CLAIMS = frozenset({"sub", "type", "tenant_id", "roles", "iat", "exp"})


class JwtTokenService:
    def __init__(self) -> None:
        self._secret = settings.jwt_secret
        self._alg = settings.jwt_algorithm

    def create_access_token(
        self, user_id: uuid.UUID, roles: list[str], tenant_id: uuid.UUID | None = None
    ) -> str:
        now = utcnow()
        payload = {
            "sub": str(user_id),
            "type": "access",
            # Present even when null. The claim's presence is the contract; its
            # value stays null until a tenant model exists (ADR-0011 §8). Sourcing
            # it from the X-Tenant-ID header would be a forgery vector, which is
            # exactly why the ADR moves it into the signed token.
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            # Role CODES only. Never permissions.
            "roles": list(roles),
            "iat": now,
            "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        }
        return jwt.encode(payload, self._secret, algorithm=self._alg)

    def create_refresh_token(self, user_id: uuid.UUID, family_id: uuid.UUID) -> tuple[str, str]:
        now = utcnow()
        payload = {
            "sub": str(user_id),
            "type": "refresh",
            "family_id": str(family_id),
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(seconds=settings.refresh_token_ttl_seconds),
        }
        raw = jwt.encode(payload, self._secret, algorithm=self._alg)
        return raw, self.hash_token(raw)

    def decode(self, token: str) -> dict:
        return jwt.decode(token, self._secret, algorithms=[self._alg])

    def decode_access(self, token: str) -> AccessTokenClaims:
        """Decode and fully validate an access token, or raise.

        Every failure below is a malformed or unauthorized CREDENTIAL, not a
        server fault, so each maps to 401. ADR-0011 §6: invalid authorization
        data must never become elevated authority — so this either returns claims
        that passed every check, or it raises. There is no partial trust.

        Note the algorithm is pinned to the configured one, so a token whose
        header claims `alg: none` (or any other algorithm) is rejected by PyJWT
        before any claim is read.
        """
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._alg])
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError("توکن منقضی شده است.") from exc
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("توکن نامعتبر است.") from exc

        if payload.get("type") != "access":
            raise UnauthorizedError("نوع توکن نامعتبر است.")

        try:
            subject_id = uuid.UUID(payload["sub"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise UnauthorizedError("توکن نامعتبر است.") from exc

        # PRESENCE is mandatory; the value may be null until tenancy exists.
        if "tenant_id" not in payload:
            raise UnauthorizedError("توکن نامعتبر است.")
        raw_tenant = payload["tenant_id"]
        if raw_tenant is None:
            tenant_id = None
        else:
            try:
                tenant_id = uuid.UUID(raw_tenant)
            except (TypeError, ValueError, AttributeError) as exc:
                raise UnauthorizedError("توکن نامعتبر است.") from exc

        # Shape only. Whether a code is KNOWN is decided during permission
        # resolution, where an unrecognised code simply grants nothing; a
        # structurally corrupt claim, by contrast, means the credential itself is
        # untrustworthy and the whole token is refused.
        raw_roles = payload.get("roles", [])
        if not isinstance(raw_roles, list) or not all(
            isinstance(role, str) for role in raw_roles
        ):
            raise UnauthorizedError("توکن نامعتبر است.")

        return AccessTokenClaims(
            subject_id=subject_id,
            tenant_id=tenant_id,
            role_codes=tuple(raw_roles),
        )

    def hash_token(self, raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()
