"""Inventory domain model — the first hard-consistency aggregate.

InventoryItem is the consistency boundary: available = on_hand - reserved, and
all three operations enforce non-negativity / no-oversell in the aggregate.
`version` carries the optimistic lock; the repository checks it on write and the
DB CHECK constraint (reserved <= on_hand) is the last-line backstop.

Reservation tracks lifecycle (pending/committed/released/expired) and is the
idempotency key for retries and relay replays.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from src.modules.inventory.domain.exceptions import (
    InsufficientStock,
    InvalidReservationState,
    InventoryDomainError,
)
from src.shared_kernel.domain.base import AggregateRoot, DomainEvent


# --- events (PII-free) -----------------------------------------------------
@dataclass(frozen=True)
class InventoryReserved(DomainEvent):
    reservation_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    location: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class InventoryCommitted(DomainEvent):
    reservation_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    location: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class InventoryReleased(DomainEvent):
    reservation_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    location: str = ""
    quantity: int = 0
    reason: str = "manual"


@dataclass(frozen=True)
class InventoryExpired(DomainEvent):
    """TTL lapsed on a held reservation. Deliberately NOT InventoryReleased:
    consumers subscribe by event_type, and a hold the clock took away demands the
    opposite saga reaction to a hold the saga itself released."""

    reservation_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    location: str = ""
    quantity: int = 0


@dataclass(frozen=True)
class InventoryReservationFailed(DomainEvent):
    reservation_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None
    location: str = ""
    quantity: int = 0
    reason: str = "insufficient_stock"


# --- Reservation -----------------------------------------------------------
class ReservationStatus:
    PENDING = "pending"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"
    ALL = frozenset({PENDING, COMMITTED, RELEASED, EXPIRED})


class Reservation:
    def __init__(
        self,
        *,
        id: uuid.UUID,
        offer_id: uuid.UUID,
        location: str,
        quantity: int,
        status: str = ReservationStatus.PENDING,
        expires_at: datetime | None = None,
    ) -> None:
        if quantity <= 0:
            raise InventoryDomainError("quantity must be positive")
        self.id = id
        self.offer_id = offer_id
        self.location = location
        self.quantity = quantity
        self.status = status
        self.expires_at = expires_at

    def commit(self) -> None:
        if self.status == ReservationStatus.COMMITTED:
            return  # idempotent
        if self.status != ReservationStatus.PENDING:
            raise InvalidReservationState("only a pending reservation can be committed")
        self.status = ReservationStatus.COMMITTED

    def release(self) -> None:
        if self.status in (ReservationStatus.RELEASED, ReservationStatus.EXPIRED):
            return  # idempotent
        if self.status == ReservationStatus.COMMITTED:
            raise InvalidReservationState("cannot release a committed reservation")
        self.status = ReservationStatus.RELEASED

    def expire(self) -> None:
        if self.status != ReservationStatus.PENDING:
            return  # only pending reservations expire
        self.status = ReservationStatus.EXPIRED


# --- InventoryItem aggregate root ------------------------------------------
class InventoryItem(AggregateRoot):
    def __init__(
        self,
        *,
        id: uuid.UUID,
        offer_id: uuid.UUID,
        location: str,
        on_hand: int = 0,
        reserved: int = 0,
        version: int = 0,
    ) -> None:
        super().__init__()
        self.id = id
        self.offer_id = offer_id
        self.location = location
        self.on_hand = on_hand
        self.reserved = reserved
        self.version = version
        self._check_invariants()

    @classmethod
    def create(cls, *, offer_id: uuid.UUID, location: str) -> "InventoryItem":
        from src.shared_kernel.domain.base import new_uuid

        return cls(id=new_uuid(), offer_id=offer_id, location=location)

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved

    def receive(self, qty: int) -> None:
        if qty <= 0:
            raise InventoryDomainError("receive quantity must be positive")
        self.on_hand += qty
        self._check_invariants()

    def reserve(self, qty: int, reservation_id: uuid.UUID) -> None:
        if qty <= 0:
            raise InventoryDomainError("reserve quantity must be positive")
        if self.available < qty:
            raise InsufficientStock()
        self.reserved += qty
        self._check_invariants()
        self.record_event(
            InventoryReserved(
                reservation_id=reservation_id, offer_id=self.offer_id,
                location=self.location, quantity=qty,
            )
        )

    def commit(self, qty: int, reservation_id: uuid.UUID) -> None:
        if qty <= 0 or self.reserved < qty:
            raise InvalidReservationState("cannot commit more than reserved")
        self.on_hand -= qty
        self.reserved -= qty
        self._check_invariants()
        self.record_event(
            InventoryCommitted(
                reservation_id=reservation_id, offer_id=self.offer_id,
                location=self.location, quantity=qty,
            )
        )

    def release(self, qty: int, reservation_id: uuid.UUID, reason: str = "manual") -> None:
        if qty <= 0 or self.reserved < qty:
            raise InvalidReservationState("cannot release more than reserved")
        self.reserved -= qty
        self._check_invariants()
        self.record_event(
            InventoryReleased(
                reservation_id=reservation_id, offer_id=self.offer_id,
                location=self.location, quantity=qty, reason=reason,
            )
        )

    def expire_reservation(self, qty: int, reservation_id: uuid.UUID) -> None:
        """Same counter effect as release, different published fact."""
        if qty <= 0 or self.reserved < qty:
            raise InvalidReservationState("cannot expire more than reserved")
        self.reserved -= qty
        self._check_invariants()
        self.record_event(
            InventoryExpired(
                reservation_id=reservation_id, offer_id=self.offer_id,
                location=self.location, quantity=qty,
            )
        )

    def fail_reservation(self, reservation_id: uuid.UUID, qty: int, reason: str) -> None:
        """Records a failed-reservation fact without changing counters. Lets the
        saga learn a reservation could not be fulfilled (event-driven reply)."""
        self.record_event(
            InventoryReservationFailed(
                reservation_id=reservation_id, offer_id=self.offer_id,
                location=self.location, quantity=qty, reason=reason,
            )
        )

    def _check_invariants(self) -> None:
        if self.on_hand < 0 or self.reserved < 0 or self.reserved > self.on_hand:
            raise InventoryDomainError(
                f"invariant violated: on_hand={self.on_hand}, reserved={self.reserved}"
            )
