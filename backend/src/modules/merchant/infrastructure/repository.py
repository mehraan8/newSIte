"""SQLAlchemy adapters for Merchant. Returns domain objects only; never commits.

Optimistic concurrency follows the repository convention established by Ordering
and Inventory: a version-guarded UPDATE whose rowcount is checked, surfacing a
lost race as ConcurrencyConflictError for the caller to retry.

The one difference from Ordering is WHERE the version is incremented. Ordering's
repository stamps `version + 1`; here the aggregate has already incremented it,
because a seller's version is published in its events and must be known when the
event is recorded (ADR-0011 §10.2). So this CASes on the pre-image, `version - 1`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.merchant.domain.entities import (
    Seller,
    SellerMember,
    SellerMemberRole,
    SellerMemberStatus,
    SellerStatus,
)
from src.modules.merchant.infrastructure.models import SellerMemberModel, SellerModel
from src.shared_kernel.application.errors import ConcurrencyConflictError, ConflictError
from src.shared_kernel.application.unit_of_work import Tracker
from src.shared_kernel.domain.base import utcnow


def _seller_to_domain(row: SellerModel) -> Seller:
    return Seller(
        id=row.id,
        code=row.code,
        name=row.name,
        status=SellerStatus(row.status),
        tenant_id=row.tenant_id,
        version=row.version,
    )


def _member_to_domain(row: SellerMemberModel) -> SellerMember:
    return SellerMember(
        seller_id=row.seller_id,
        user_id=row.user_id,
        role=SellerMemberRole(row.member_role_code),
        status=SellerMemberStatus(row.status),
    )


class SqlSellerRepository:
    def __init__(self, session: AsyncSession, tracker: Tracker | None = None) -> None:
        self._s = session
        self._tracker = tracker

    def _track(self, seller: Seller) -> None:
        if self._tracker is not None:
            self._tracker.track(seller)

    async def get(self, seller_id: uuid.UUID) -> Seller | None:
        row = await self._s.get(SellerModel, seller_id)
        if row is None:
            return None
        seller = _seller_to_domain(row)
        self._track(seller)
        return seller

    async def get_by_code(self, code: str) -> Seller | None:
        row = await self._s.scalar(select(SellerModel).where(SellerModel.code == code))
        if row is None:
            return None
        seller = _seller_to_domain(row)
        self._track(seller)
        return seller

    async def add(self, seller: Seller) -> None:
        self._s.add(
            SellerModel(
                id=seller.id,
                code=seller.code,
                name=seller.name,
                status=seller.status.value,
                tenant_id=seller.tenant_id,
                version=seller.version,
            )
        )
        try:
            await self._s.flush()
        except IntegrityError as exc:  # uq_sellers_code
            raise ConflictError("فروشنده‌ای با این کد از قبل وجود دارد.") from exc
        self._track(seller)

    async def save(self, seller: Seller) -> None:
        result = await self._s.execute(
            update(SellerModel)
            .where(
                SellerModel.id == seller.id,
                # The aggregate already bumped `version`, so the row must still
                # hold the pre-image for this write to be the winner.
                SellerModel.version == seller.version - 1,
            )
            .values(
                code=seller.code,
                name=seller.name,
                status=seller.status.value,
                tenant_id=seller.tenant_id,
                version=seller.version,
                updated_at=utcnow(),
            )
        )
        if result.rowcount == 0:
            raise ConcurrencyConflictError("تداخل همزمانی در ویرایش فروشنده.")
        self._track(seller)


class SqlSellerMemberRepository:
    def __init__(self, session: AsyncSession, tracker: Tracker | None = None) -> None:
        self._s = session
        self._tracker = tracker

    def _track(self, member: SellerMember) -> None:
        if self._tracker is not None:
            self._tracker.track(member)

    async def get(
        self, seller_id: uuid.UUID, user_id: uuid.UUID
    ) -> SellerMember | None:
        row = await self._s.get(SellerMemberModel, (seller_id, user_id))
        if row is None:
            return None
        member = _member_to_domain(row)
        self._track(member)
        return member

    async def list_for_user(self, user_id: uuid.UUID) -> list[SellerMember]:
        rows = await self._s.scalars(
            select(SellerMemberModel).where(SellerMemberModel.user_id == user_id)
        )
        return [_member_to_domain(r) for r in rows.all()]

    async def list_for_seller(self, seller_id: uuid.UUID) -> list[SellerMember]:
        rows = await self._s.scalars(
            select(SellerMemberModel).where(SellerMemberModel.seller_id == seller_id)
        )
        return [_member_to_domain(r) for r in rows.all()]

    async def add(self, member: SellerMember) -> None:
        self._s.add(
            SellerMemberModel(
                seller_id=member.seller_id,
                user_id=member.user_id,
                member_role_code=member.role.value,
                status=member.status.value,
            )
        )
        try:
            await self._s.flush()
        except IntegrityError as exc:  # pk_seller_members
            raise ConflictError(
                "این کاربر از قبل عضو این فروشنده است."
            ) from exc
        self._track(member)

    async def save(self, member: SellerMember) -> None:
        await self._s.execute(
            update(SellerMemberModel)
            .where(
                SellerMemberModel.seller_id == member.seller_id,
                SellerMemberModel.user_id == member.user_id,
            )
            .values(
                member_role_code=member.role.value,
                status=member.status.value,
                updated_at=utcnow(),
            )
        )
        self._track(member)
