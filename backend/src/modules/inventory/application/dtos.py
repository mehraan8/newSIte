"""Inventory command/result DTOs."""

from __future__ import annotations

from dataclasses import dataclass

from src.modules.inventory.domain.entities import InventoryItem, Reservation


@dataclass(frozen=True)
class ReceiveStockCommand:
    offer_id: str
    location: str
    quantity: int


@dataclass(frozen=True)
class ReserveStockCommand:
    reservation_id: str        # caller-supplied idempotency key
    offer_id: str
    location: str
    quantity: int
    ttl_seconds: int = 900     # reservation hold window


@dataclass(frozen=True)
class StockView:
    offer_id: str
    location: str
    on_hand: int
    reserved: int
    available: int

    @classmethod
    def from_domain(cls, item: InventoryItem) -> "StockView":
        return cls(
            offer_id=str(item.offer_id), location=item.location,
            on_hand=item.on_hand, reserved=item.reserved, available=item.available,
        )


@dataclass(frozen=True)
class ReservationView:
    id: str
    offer_id: str
    location: str
    quantity: int
    status: str

    @classmethod
    def from_domain(cls, r: Reservation) -> "ReservationView":
        return cls(
            id=str(r.id), offer_id=str(r.offer_id), location=r.location,
            quantity=r.quantity, status=r.status,
        )
