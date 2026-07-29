"""Selling write-side domain model: the Offer aggregate.

First financial domain → correctness-critical:
  * price NEVER mutates outside the aggregate (only via change_price)
  * every price change is an event (OfferPriceChanged)
  * activation requires a price; events carry enough for BuyBox ranking later
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.shared_kernel.domain.base import AggregateRoot, DomainEvent, new_uuid
from src.shared_kernel.domain.value_objects import Money
from src.modules.selling.domain.exceptions import (
    InvalidOfferTransition,
    OfferNotPriced,
    PriceCurrencyMismatch,
)


class OfferStatus:
    DRAFT = "draft"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ALL = frozenset({DRAFT, ACTIVE, SUSPENDED})


class OfferCondition:
    NEW = "new"
    REFURBISHED = "refurbished"
    USED = "used"
    ALL = frozenset({NEW, REFURBISHED, USED})


# --- Domain events (PII-free; BuyBox-ready) --------------------------------
@dataclass(frozen=True)
class OfferCreated(DomainEvent):
    offer_id: uuid.UUID | None = None
    seller_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None
    condition: str = ""
    currency: str = ""
    status: str = OfferStatus.DRAFT


@dataclass(frozen=True)
class OfferActivated(DomainEvent):
    offer_id: uuid.UUID | None = None
    seller_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None
    condition: str = ""
    currency: str = ""
    # Enough for BuyBox landed-price ranking later:
    price_amount: int | None = None
    price_scale: int = 0
    compare_at_amount: int | None = None


@dataclass(frozen=True)
class OfferPriceChanged(DomainEvent):
    offer_id: uuid.UUID | None = None
    seller_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None
    currency: str = ""
    old_amount: int | None = None
    new_amount: int | None = None
    scale: int = 0


@dataclass(frozen=True)
class OfferSuspended(DomainEvent):
    offer_id: uuid.UUID | None = None
    seller_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None


# --- Offer aggregate root --------------------------------------------------
class Offer(AggregateRoot):
    def __init__(
        self,
        *,
        id: uuid.UUID,
        seller_id: uuid.UUID,
        variant_id: uuid.UUID,
        condition: str,
        currency: str,
        status: str = OfferStatus.DRAFT,
        current_price: Money | None = None,
        compare_at: Money | None = None,
    ) -> None:
        super().__init__()
        if condition not in OfferCondition.ALL:
            raise InvalidOfferTransition(f"invalid condition {condition!r}")
        if status not in OfferStatus.ALL:
            raise InvalidOfferTransition(f"invalid status {status!r}")
        if len(currency) != 3:
            raise PriceCurrencyMismatch("currency must be a 3-letter code")
        self.id = id
        self.seller_id = seller_id
        self.variant_id = variant_id
        self.condition = condition
        self.currency = currency
        self.status = status
        self.current_price = current_price
        self.compare_at = compare_at

    # -- factory --
    @classmethod
    def create(
        cls,
        *,
        seller_id: uuid.UUID,
        variant_id: uuid.UUID,
        condition: str,
        currency: str,
        initial_price: Money | None = None,
        compare_at: Money | None = None,
    ) -> "Offer":
        offer = cls(
            id=new_uuid(),
            seller_id=seller_id,
            variant_id=variant_id,
            condition=condition,
            currency=currency,
            status=OfferStatus.DRAFT,
        )
        offer.record_event(
            OfferCreated(
                offer_id=offer.id,
                seller_id=seller_id,
                variant_id=variant_id,
                condition=condition,
                currency=currency,
                status=OfferStatus.DRAFT,
            )
        )
        if initial_price is not None:
            offer.change_price(initial_price, compare_at=compare_at)
        return offer

    # -- behavior: the ONLY price mutation path --
    def change_price(self, new_price: Money, *, compare_at: Money | None = None) -> None:
        if new_price.currency != self.currency:
            raise PriceCurrencyMismatch(
                f"offer currency {self.currency}, price currency {new_price.currency}"
            )
        if compare_at is not None and compare_at.currency != self.currency:
            raise PriceCurrencyMismatch("compare_at currency mismatch")
        old = self.current_price
        self.current_price = new_price
        self.compare_at = compare_at
        self.record_event(
            OfferPriceChanged(
                offer_id=self.id,
                seller_id=self.seller_id,
                variant_id=self.variant_id,
                currency=self.currency,
                old_amount=old.amount if old else None,
                new_amount=new_price.amount,
                scale=new_price.scale,
            )
        )

    def activate(self) -> None:
        if self.current_price is None:
            raise OfferNotPriced()
        if self.status == OfferStatus.ACTIVE:
            return  # idempotent — no event
        self.status = OfferStatus.ACTIVE
        self.record_event(
            OfferActivated(
                offer_id=self.id,
                seller_id=self.seller_id,
                variant_id=self.variant_id,
                condition=self.condition,
                currency=self.currency,
                price_amount=self.current_price.amount,
                price_scale=self.current_price.scale,
                compare_at_amount=self.compare_at.amount if self.compare_at else None,
            )
        )

    def suspend(self) -> None:
        if self.status != OfferStatus.ACTIVE:
            raise InvalidOfferTransition("only an active offer can be suspended")
        self.status = OfferStatus.SUSPENDED
        self.record_event(
            OfferSuspended(offer_id=self.id, seller_id=self.seller_id, variant_id=self.variant_id)
        )
