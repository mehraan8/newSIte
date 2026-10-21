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


class SqlUserStatusQuery:
    """One indexed column, on the authentication hot path.

    `ix_users_status` is irrelevant here — this is a primary-key lookup, which is
    what makes a per-request account-status check affordable. ADR-0011 §8 accepts
    stale ROLES for the life of an access token; it does not accept a stale
    ACCOUNT, because a blocked account must stop working at once (§16.1).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def status_of(self, user_id: uuid.UUID) -> str | None:
        return await self._s.scalar(
            select(UserModel.status).where(UserModel.id == user_id)
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

    async def rotate(self, old_hash: str, new_hash: str, ttl: int) -> bool:
        """Atomically consume `old_hash` and issue `new_hash`. False if it was
        already consumed.

        This was a read-then-write: SELECT the row, set `revoked_at`, INSERT the
        child. Two requests presenting the SAME token concurrently both passed
        the read before either wrote, and both got a token — a single-use
        credential spent N times, producing N live siblings. Verified: ten
        parallel refreshes of one token returned ten valid tokens.

        That is worse than it looks. Reuse detection (D1) fires when a token that
        is no longer in the table is presented again. Under the race the thief
        and the victim each walk away with a DIFFERENT valid token, so neither
        ever replays a consumed one, and the theft is never detected. The race
        did not merely duplicate a token; it disabled the alarm.

        The fix is one conditional UPDATE. `revoked_at IS NULL` makes the claim
        the same operation as the check, and Postgres serialises the two writers
        on the row: the loser re-evaluates the predicate after the winner commits
        and matches zero rows. RETURNING carries the family across so the caller
        does not need a second read.
        """
        claimed = (
            await self._s.execute(
                update(RefreshTokenModel)
                .where(
                    RefreshTokenModel.token_hash == old_hash,
                    RefreshTokenModel.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow())
                .returning(RefreshTokenModel.user_id, RefreshTokenModel.family_id)
            )
        ).first()
        if claimed is None:
            return False

        user_id, family_id = claimed
        self._s.add(
            RefreshTokenModel(
                user_id=user_id,
                token_hash=new_hash,
                family_id=family_id,
                expires_at=utcnow() + timedelta(seconds=ttl),
            )
        )
        await self._s.flush()
        return True

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
