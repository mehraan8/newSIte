"""Inventory ports. Repository raises ConcurrencyConflictError on optimistic-lock
or unique-violation so use cases can retry."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from src.modules.inventory.domain.entities import InventoryItem, Reservation


class InventoryRepository(Protocol):
    async def get_item(self, offer_id: uuid.UUID, location: str) -> InventoryItem | None: ...
    async def add_item(self, item: InventoryItem) -> None: ...
    async def save_item(self, item: InventoryItem) -> None: ...  # optimistic; may raise conflict
    async def get_reservation(self, reservation_id: uuid.UUID) -> Reservation | None: ...
    async def add_reservation(self, reservation: Reservation) -> None: ...  # may raise conflict

    async def save_reservation(
        self, reservation: Reservation, *, expected_status: str
    ) -> None:
        """Compare-and-set on status. `expected_status` is the status the caller
        READ before mutating; a mismatch means someone else transitioned the
        reservation first and raises ConcurrencyConflictError."""
        ...

    async def find_expired_pending(self, now: datetime, limit: int = 100) -> list[Reservation]:
        """Claims rows FOR UPDATE SKIP LOCKED — the lock lives until the calling
        UoW ends, so the caller must process them in the SAME transaction. Callers
        wanting per-reservation isolation pass limit=1 and loop over UoWs."""
        ...


class SellingGateway(Protocol):
    """Read-only ACL to Selling — the only way Inventory references an offer."""

    async def offer_exists(self, offer_id: uuid.UUID) -> bool: ...

    async def seller_of(self, offer_id: uuid.UUID) -> uuid.UUID | None:
        """The seller that owns the offer, or None if the offer does not exist.

        Stock is anchored on `offer_id` and carries no seller of its own, so this
        is how Inventory learns who a receive would credit. Reading it through
        the existing ACL rather than joining to `offers` keeps the boundary
        intact: on extraction this becomes one HTTP call, not a schema change.

        ADR-0011 §9 records `inventory_stock.seller_id` as the TARGET STATE
        denormalization. Until that column exists this resolves per request; the
        authorization semantics are identical either way.
        """
        ...
