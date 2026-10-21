"""Ordering command/result DTOs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from src.modules.ordering.domain.entities import Order


@dataclass(frozen=True)
class OrderLineInput:
    """What the caller may choose: WHICH offer, from WHERE, HOW MANY.

    Deliberately no price. The caller does not get a say in what things cost —
    PlaceOrder resolves that through OfferPricingGateway.
    """

    offer_id: str
    location: str
    quantity: int


@dataclass(frozen=True)
class PlaceOrderCommand:
    """`customer_id` is populated by the interface layer from the AUTHENTICATED
    principal, never from the request body. It is on the command because the
    application layer needs it; it is not an input the caller controls."""

    customer_id: str
    region: str
    currency: str
    lines: tuple[OrderLineInput, ...]
    # Optional client-supplied dedup key (the Idempotency-Key header). When
    # absent the endpoint behaves as before — see the router for why this is
    # opt-in rather than mandatory.
    idempotency_key: str | None = None

    def fingerprint(self) -> str:
        """Canonical hash of what was ordered. Replaying a key with a different
        body must not silently return the first order, so the two are compared.
        Line ORDER is not significant; quantities and offers are."""
        canonical = "|".join(
            [
                self.customer_id, self.region, self.currency,
                *sorted(
                    f"{li.offer_id}:{li.location}:{li.quantity}" for li in self.lines
                ),
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OrderLineView:
    offer_id: str
    quantity: int
    unit_amount: int
    reservation_id: str
    status: str


@dataclass(frozen=True)
class OrderView:
    id: str
    customer_id: str
    region: str
    currency: str
    status: str
    total_amount: int
    lines: tuple[OrderLineView, ...]

    @classmethod
    def from_domain(cls, order: Order) -> "OrderView":
        return cls(
            id=str(order.id), customer_id=str(order.customer_id), region=order.region,
            currency=order.currency, status=order.status, total_amount=order.total_amount,
            lines=tuple(
                OrderLineView(
                    offer_id=str(ln.offer_id), quantity=ln.quantity, unit_amount=ln.unit_amount,
                    reservation_id=str(ln.reservation_id), status=ln.status,
                )
                for ln in order.lines
            ),
        )
