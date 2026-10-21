"""Command/result DTOs for Selling use cases."""

from __future__ import annotations

from dataclasses import dataclass

from src.modules.selling.domain.entities import Offer


@dataclass(frozen=True)
class CreateOfferCommand:
    seller_id: str
    variant_id: str
    condition: str = "new"
    currency: str = "IRR"
    amount: int | None = None        # minor units; None = create without price
    scale: int = 0
    compare_at_amount: int | None = None


@dataclass(frozen=True)
class ChangeOfferPriceCommand:
    amount: int
    currency: str = "IRR"
    scale: int = 0
    compare_at_amount: int | None = None


@dataclass(frozen=True)
class PriceView:
    amount: int
    currency: str
    scale: int
    compare_at_amount: int | None


@dataclass(frozen=True)
class OfferView:
    id: str
    seller_id: str
    variant_id: str
    condition: str
    currency: str
    status: str
    price: PriceView | None

    @classmethod
    def from_domain(cls, offer: Offer) -> "OfferView":
        price = None
        if offer.current_price is not None:
            price = PriceView(
                amount=offer.current_price.amount,
                currency=offer.current_price.currency,
                scale=offer.current_price.scale,
                compare_at_amount=offer.compare_at.amount if offer.compare_at else None,
            )
        return cls(
            id=str(offer.id),
            seller_id=str(offer.seller_id),
            variant_id=str(offer.variant_id),
            condition=offer.condition,
            currency=offer.currency,
            status=offer.status,
            price=price,
        )
