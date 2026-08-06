"""SQLAlchemy adapters implementing the Identity ports."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.identity.domain.entities import User
from src.modules.identity.infrastructure.models import RefreshTokenModel, UserModel
from src.shared_kernel.application.unit_of_work import Tracker
from src.shared_kernel.domain.base import utcnow
from src.shared_kernel.domain.value_objects import Mobile


def _to_domain(row: UserModel) -> User:
    return User(
        id=row.id,
        mobile=Mobile(row.mobile),
        full_name=row.full_name,
        email=row.email,
        status=row.status,
        is_phone_verified=row.is_phone_verified,
    )


class SqlUserRepository:
    def __init__(self, session: AsyncSession, tracker: Tracker | None = None) -> None:
        self._s = session
        self._tracker = tracker

    def _track(self, user: User) -> None:
        # Register the aggregate so the UoW can drain its events on commit.
        if self._tracker is not None:
            self._tracker.track(user)

    async def get_by_mobile(self, mobile: Mobile) -> User | None:
        row = await self._s.scalar(select(UserModel).where(UserModel.mobile == str(mobile)))
        if row is None:
            return None
        user = _to_domain(row)
        self._track(user)
        return user

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        row = await self._s.get(UserModel, user_id)
        if row is None:
            return None
        user = _to_domain(row)
        self._track(user)
        return user

    async def add(self, user: User) -> None:
        self._s.add(
            UserModel(
                id=user.id,
                mobile=str(user.mobile),
                full_name=user.full_name,
                email=user.email,
                status=user.status,
                is_phone_verified=user.is_phone_verified,
            )
        )
        self._track(user)
        await self._s.flush()

    async def update(self, user: User) -> None:
        await self._s.execute(
            update(UserModel)
            .where(UserModel.id == user.id)
            .values(
                full_name=user.full_name,
                email=user.email,
                status=user.status,
                is_phone_verified=user.is_phone_verified,
                last_login_at=utcnow(),
            )
        )


class SqlRefreshTokenStore:
    """Persists refresh tokens in Postgres for durable rotation/reuse detection."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def save(
        self, user_id: uuid.UUID, token_hash: str, family_id: uuid.UUID, ttl: int
    ) -> None:
        self._s.add(
            RefreshTokenModel(
                user_id=user_id,
                token_hash=token_hash,
                family_id=family_id,
                expires_at=utcnow() + timedelta(seconds=ttl),
            )
        )
        await self._s.flush()

    async def is_valid(self, token_hash: str) -> bool:
        return (await self.get_meta(token_hash)) is not None

    async def get_meta(self, token_hash: str) -> dict | None:
        row = await self._s.scalar(
            select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        )
        if row is None or row.revoked_at is not None or row.expires_at <= utcnow():
            return None
        return {"user_id": str(row.user_id), "family_id": str(row.family_id)}

    async def rotate(self, old_hash: str, new_hash: str, ttl: int) -> None:
        row = await self._s.scalar(
            select(RefreshTokenModel).where(RefreshTokenModel.token_hash == old_hash)
        )
        if row is None:
            return
        row.revoked_at = utcnow()
        self._s.add(
            RefreshTokenModel(
                user_id=row.user_id,
                token_hash=new_hash,
                family_id=row.family_id,
                expires_at=utcnow() + timedelta(seconds=ttl),
            )
        )
        await self._s.flush()

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        await self._s.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.family_id == family_id, RefreshTokenModel.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        # Commit HERE, not at the end of the request. Reuse detection revokes the
        # family and then raises UnauthorizedError; the request-scoped session
        # (see shared_kernel/infrastructure/database.py:get_session) rolls back on
        # any exception, which silently discarded this revocation and left the
        # stolen family fully usable. This is the only write in that code path, so
        # committing it here makes the security guarantee real without affecting
        # anything else staged on the session.
        await self._s.commit()
