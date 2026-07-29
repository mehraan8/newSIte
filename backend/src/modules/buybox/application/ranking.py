"""BuyBox ranking (v1) — pure, DB-free, deterministic.

Rules: active + priced offers only; lowest landed price wins; ties broken by
offer_created_at, then offer_id (absolute determinism for replay).

Prices are compared exactly as rationals over minor units, never as floats, so
offers quoted at a different `scale` rank correctly at any magnitude.

Currencies are never compared to each other. Following `Money`'s "no implicit
FX" rule, the reference currency of a (variant, region) is the incumbent's — the
currency of its earliest-created active priced offer — and offers quoted in any
other currency are left out of the ranking rather than silently mis-ranked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

_TEN = Fraction(10)


@dataclass
class OfferProjection:
    """BuyBox's own derived view of one offer (built only from events)."""

    offer_id: uuid.UUID
    variant_id: uuid.UUID
    seller_id: uuid.UUID
    region: str
    offer_created_at: datetime
    is_active: bool = False
    currency: str | None = None
    amount: int | None = None      # minor units; None until priced
    scale: int = 0


def _landed(offer: OfferProjection) -> Fraction:
    """Exact price in major units — no float, no precision ceiling."""
    return offer.amount * _TEN ** -offer.scale


def _sequence(offer: OfferProjection) -> tuple[datetime, str]:
    """Total order over offers that never depends on arrival/row order."""
    return (offer.offer_created_at, str(offer.offer_id))


def _sellable(offers: list[OfferProjection]) -> list[OfferProjection]:
    return [o for o in offers if o.is_active and o.amount is not None and o.currency]


def reference_currency(offers: list[OfferProjection]) -> str | None:
    """The incumbent's currency, or None when nothing is sellable."""
    sellable = _sellable(offers)
    if not sellable:
        return None
    return min(sellable, key=_sequence).currency


def pick_winner(offers: list[OfferProjection]) -> OfferProjection | None:
    reference = reference_currency(offers)
    if reference is None:
        return None
    candidates = [o for o in _sellable(offers) if o.currency == reference]
    return min(candidates, key=lambda o: (_landed(o), _sequence(o)))
