"""Inventory domain errors (HTTP-agnostic; translated at the boundary)."""

from __future__ import annotations


class InventoryDomainError(Exception):
    message_fa = "خطا در موجودی."

    def __init__(self, message_fa: str | None = None) -> None:
        self.message_fa = message_fa or self.message_fa
        super().__init__(self.message_fa)


class InsufficientStock(InventoryDomainError):
    message_fa = "موجودی کافی نیست."


class InvalidReservationState(InventoryDomainError):
    message_fa = "وضعیت رزرو برای این عملیات معتبر نیست."
