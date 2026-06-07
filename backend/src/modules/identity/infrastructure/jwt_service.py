"""JWT issuing/verification. Access tokens are short-lived & stateless; refresh
tokens are opaque-ish JWTs whose hash is tracked server-side for rotation.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import jwt

from src.config.settings import settings
from src.shared_kernel.domain.base import utcnow


class JwtTokenService:
    def __init__(self) -> None:
        self._secret = settings.jwt_secret
        self._alg = settings.jwt_algorithm

    def create_access_token(self, user_id: uuid.UUID, roles: list[str]) -> str:
        now = utcnow()
        payload = {
            "sub": str(user_id),
            "type": "access",
            "roles": roles,
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

    def hash_token(self, raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()
