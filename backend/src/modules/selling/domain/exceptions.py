"""Selling domain errors — pure domain, HTTP-agnostic (translated at the boundary)."""

from __future__ import annotations


class SellingDomainError(Exception):
    message_fa = "خطا در دامنهٔ فروش."

    def __init__(self, message_fa: str | None = None) -> None:
        self.message_fa = message_fa or self.message_fa
        super().__init__(self.message_fa)


class OfferNotPriced(SellingDomainError):
    message_fa = "آفر بدون قیمت قابل فعال‌سازی نیست."


class PriceCurrencyMismatch(SellingDomainError):
    message_fa = "ارز قیمت با ارز آفر هم‌خوان نیست."


class InvalidOfferTransition(SellingDomainError):
    message_fa = "تغییر وضعیت آفر مجاز نیست."
