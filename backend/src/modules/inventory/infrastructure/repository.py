"""SQLAlchemy adapter for Inventory. Optimistic locking on save_item via a
version-guarded UPDATE; unique/version conflicts surface as
ConcurrencyConflictError for the use-case retry loop. Returns domain objects only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.inventory.domain.entities import (
    InventoryItem,
    Reservation,
    ReservationStatus,
)
from src.modules.inventory.infrastructure.models import InventoryStockModel, ReservationModel
from src.shared_kernel.application.errors import ConcurrencyConflictError
from src.shared_kernel.application.unit_of_work import Tracker
from src.shared_kernel.domain.base import utcnow


def _item_to_domain(row: InventoryStockModel) -> InventoryItem:
    return InventoryItem(
        id=row.id, offer_id=row.offer_id, location=row.location,
        on_hand=row.on_hand, reserved=row.reserved, version=row.version,
    )


def _res_to_domain(row: ReservationModel) -> Reservation:
    return Reservation(
        id=row.id, offer_id=row.offer_id, location=row.location,
        quantity=row.quantity, status=row.status, expires_at=row.expires_at,
    )


class SqlInventoryRepository:
    def __init__(self, session: AsyncSession, tracker: Tracker | None = None) -> None:
        self._s = session
        self._tracker = tracker

    def _track(self, item: InventoryItem) -> None:
        if self._tracker is not None:
            self._tracker.track(item)

    # -- inventory item --
    async def get_item(self, offer_id: uuid.UUID, location: str) -> InventoryItem | None:
        row = await self._s.scalar(
            select(InventoryStockModel).where(
                InventoryStockModel.offer_id == offer_id,
                InventoryStockModel.location == location,
            )
        )
        return _item_to_domain(row) if row else None

    async def add_item(self, item: InventoryItem) -> None:
        self._s.add(
            InventoryStockModel(
                id=item.id, offer_id=item.offer_id, location=item.location,
                on_hand=item.on_hand, reserved=item.reserved, version=item.version,
            )
        )
        try:
            await self._s.flush()
        except IntegrityError as exc:  # unique(offer,location) race or CHECK
            raise ConcurrencyConflictError() from exc
        self._track(item)

    async def save_item(self, item: InventoryItem) -> None:
        # Optimistic, version-guarded write — atomic compare-and-set.
        try:
            result = await self._s.execute(
                update(InventoryStockModel)
                .where(
                    InventoryStockModel.id == item.id,
                    InventoryStockModel.version == item.version,
                )
                .values(
                    on_hand=item.on_hand,
                    reserved=item.reserved,
                    version=item.version + 1,
                    updated_at=utcnow(),
                )
            )
        except IntegrityError as exc:  # CHECK reserved<=on_hand backstop
            raise ConcurrencyConflictError("inventory constraint violated") from exc
        if result.rowcount == 0:
            raise ConcurrencyConflictError("inventory version conflict")
        item.version += 1
        self._track(item)

    # -- reservation --
    async def get_reservation(self, reservation_id: uuid.UUID) -> Reservation | None:
        row = await self._s.get(ReservationModel, reservation_id)
        return _res_to_domain(row) if row else None

    async def add_reservation(self, reservation: Reservation) -> None:
        self._s.add(
            ReservationModel(
                id=reservation.id, offer_id=reservation.offer_id, location=reservation.location,
                quantity=reservation.quantity, status=reservation.status,
                expires_at=reservation.expires_at,
            )
        )
        try:
            await self._s.flush()
        except IntegrityError as exc:  # duplicate reservation_id (idempotency race)
            raise ConcurrencyConflictError() from exc

    async def save_reservation(self, reservation: Reservation, *, expected_status: str) -> None:
        # Compare-and-set on status, mirroring save_item's version guard. Without
        # the status predicate this is a blind write and a concurrent transition
        # (commit vs release vs expire) silently overwrites the winner.
        result = await self._s.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation.id,
                ReservationModel.status == expected_status,
            )
            .values(status=reservation.status, updated_at=utcnow())
        )
        if result.rowcount == 0:
            raise ConcurrencyConflictError("reservation status conflict")

    async def find_expired_pending(self, now: datetime, limit: int = 100) -> list[Reservation]:
        # SKIP LOCKED lets parallel sweepers claim disjoint rows instead of
        # blocking on each other. The lock is released when the caller's UoW ends,
        # so the caller must process these rows in the same transaction.
        rows = await self._s.scalars(
            select(ReservationModel)
            .where(
                ReservationModel.status == ReservationStatus.PENDING,
                ReservationModel.expires_at.is_not(None),
                ReservationModel.expires_at <= now,
            )
            .order_by(ReservationModel.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_res_to_domain(r) for r in rows.all()]
