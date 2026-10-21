"""Merchant domain errors. Pure Python — no HTTP, no framework.

Translated to transport-mappable application errors at the application boundary,
exactly as Selling does with SellingDomainError.
"""

from __future__ import annotations


class MerchantDomainError(Exception):
    """Base for Merchant rule violations."""

    message_fa = "خطای دامنهٔ فروشنده."

    def __init__(self, message_fa: str | None = None) -> None:
        self.message_fa = message_fa or self.message_fa
        super().__init__(self.message_fa)


class InvalidSellerTransition(MerchantDomainError):
    message_fa = "تغییر وضعیت فروشنده مجاز نیست."


class InvalidMembershipTransition(MerchantDomainError):
    message_fa = "تغییر وضعیت عضویت مجاز نیست."


class SellerIdentityError(MerchantDomainError):
    """A Seller identity was derived from something it must never come from.

    Exists to make the `User != Seller` invariant enforceable in code rather than
    only in review (ADR-0011 §7.1).
    """

    message_fa = "شناسهٔ فروشنده نامعتبر است."
