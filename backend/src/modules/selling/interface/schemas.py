"""HTTP models for Selling endpoints."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.modules.selling.application.dtos import OfferView


class CreateOfferRequest(BaseModel):
    """No seller_id: it comes from the bearer token, exactly as Ordering derives
    customer_id. extra="forbid" so a client still sending `seller_id` gets a 422
    instead of having it silently ignored — silent ignoring hides an attempted
    impersonation, rejecting surfaces it."""

    model_config = ConfigDict(extra="forbid")

    # UUID-typed: a malformed id is a 422 from the schema rather than a ValueError
    # from uuid.UUID() inside the use case (previously a 500). Wire format unchanged.
    variant_id: uuid.UUID
    condition: str = "new"
    # Money constraints mirror the Money value object, which rejects a negative
    # amount, a negative scale and a currency that is not 3 characters.
    currency: str = Field(default="IRR", min_length=3, max_length=3)
    amount: int | None = Field(default=None, ge=0, description="price in minor units")
    scale: int = Field(default=0, ge=0, le=8)
    compare_at_amount: int | None = Field(default=None, ge=0)


class ChangeOfferPriceRequest(BaseModel):
    amount: int = Field(ge=0)
    currency: str = Field(default="IRR", min_length=3, max_length=3)
    scale: int = Field(default=0, ge=0, le=8)
    compare_at_amount: int | None = Field(default=None, ge=0)


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
