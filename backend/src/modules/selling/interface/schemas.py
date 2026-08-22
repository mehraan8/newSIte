"""HTTP models for Selling endpoints."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.modules.selling.application.dtos import OfferView


class CreateOfferRequest(BaseModel):
    """`seller_id` is a TARGET SELECTOR, not a claim of authority (ADR-0011 §7.1).

    It was previously banned outright, back when the token was the only source of
    seller identity. It no longer is: a User is not a Seller, and one User can be
    a member of several Sellers — staff at one, manager at another — so there is
    nothing to infer from and the caller has to say which seller the offer is
    for. What makes this safe is not who sends the field but who proves it: the
    use case honours the nomination only if that seller appears in
    `Principal.memberships` with `selling.offers.create`. Naming someone else's
    seller, or one's own user id, resolves to no membership and is denied.

    extra="forbid" is kept for every OTHER unexpected field, so a client sending
    a field the server does not model gets a 422 rather than silent acceptance.
    """

    model_config = ConfigDict(extra="forbid")

    # UUID-typed: a malformed id is a 422 from the schema rather than a ValueError
    # from uuid.UUID() inside the use case (previously a 500). Wire format unchanged.
    seller_id: uuid.UUID
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
