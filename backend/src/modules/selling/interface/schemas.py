"""HTTP models for Selling endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.modules.selling.application.dtos import OfferView


class CreateOfferRequest(BaseModel):
    seller_id: str
    variant_id: str
    condition: str = "new"
    currency: str = "IRR"
    amount: int | None = Field(default=None, description="price in minor units")
    scale: int = 0
    compare_at_amount: int | None = None


class ChangeOfferPriceRequest(BaseModel):
    amount: int
    currency: str = "IRR"
    scale: int = 0
    compare_at_amount: int | None = None


class PriceResponse(BaseModel):
    amount: int
    currency: str
    scale: int
    compare_at_amount: int | None


class OfferResponse(BaseModel):
    id: str
    seller_id: str
    variant_id: str
    condition: str
    currency: str
    status: str
    price: PriceResponse | None

    @classmethod
    def from_view(cls, view: OfferView) -> "OfferResponse":
        return cls(
            id=view.id,
            seller_id=view.seller_id,
            variant_id=view.variant_id,
            condition=view.condition,
            currency=view.currency,
            status=view.status,
            price=PriceResponse(**view.price.__dict__) if view.price else None,
        )
