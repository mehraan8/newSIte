"""Ordering ports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from src.modules.ordering.domain.entities import Order


class OrderRepository(Protocol):
    async def get(self, order_id: uuid.UUID) -> Order | None: ...
    async def add(self, order: Order) -> None: ...
    async def save(self, order: Order) -> None: ...
    async def find_by_reservation_id(self, reservation_id: uuid.UUID) -> Order | None: ...


@dataclass(frozen=True)
class IdempotencyRecord:
    order_id: uuid.UUID
    request_fingerprint: str


class OrderIdempotencyStore(Protocol):
    """Checkout dedup, keyed (customer_id, idempotency_key).

    `remember` writes through the caller's UoW so the key and the order commit
    together, and raises ConcurrencyConflictError on a unique violation — two
    concurrent duplicates race, the loser retries and then finds the record.
    """

    async def find(
        self, customer_id: uuid.UUID, idempotency_key: str
    ) -> IdempotencyRecord | None: ...

    async def remember(
        self,
        customer_id: uuid.UUID,
        idempotency_key: str,
        order_id: uuid.UUID,
        request_fingerprint: str,
    ) -> None: ...


@dataclass(frozen=True)
class OfferPriceSnapshot:
    """What the read model says an offer costs, right now, in one region.

    Carries `is_active` and a nullable `amount` rather than pre-filtering, so the
    ACL stays a dumb reader and sellability policy stays in the use case.
    """

    offer_id: uuid.UUID
    region: str
    currency: str | None
    amount: int | None      # minor units at `scale`; None until priced
    scale: int
    is_active: bool


class OfferPricingGateway(Protocol):
    """Read-only ACL to the pricing read model — the ONLY way Ordering learns
    what an offer costs.

    Price is never accepted from the caller: a client-supplied `unit_amount` is
    an authorization bypass (anyone buys anything for 1 rial). Ordering resolves
    it here, server-side, and snapshots the result onto the line.

    Returns None when the offer has no sellable row in that region. Becomes an
    HTTP call on extraction; no BuyBox class is ever imported.
    """

    async def price_for(
        self, offer_id: uuid.UUID, region: str
    ) -> OfferPriceSnapshot | None: ...
