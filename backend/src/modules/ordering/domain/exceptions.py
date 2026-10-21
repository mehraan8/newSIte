"""Ordering domain errors (HTTP-agnostic)."""

from __future__ import annotations


class OrderingDomainError(Exception):
    message_fa = "خطا در سفارش."

    def __init__(self, message_fa: str | None = None) -> None:
        self.message_fa = message_fa or self.message_fa
        super().__init__(self.message_fa)


class InvalidOrderTransition(OrderingDomainError):
    message_fa = "تغییر وضعیت سفارش مجاز نیست."
