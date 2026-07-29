"""BuyBox projector — applies the consumed Selling events to the read model.

Fully derived from events (no source-DB joins). After mutating an offer's
derived state it recomputes the winner for that (variant_id, region). Naturally
idempotent: each handler sets state to a value, never increments; the inbox adds
exactly-once on top.
"""

from __future__ import annotations

import uuid

from src.modules.buybox.application.ports import BuyBoxRepository
from src.modules.buybox.application.ranking import OfferProjection, pick_winner
from src.shared_kernel.infrastructure.outbox.contracts import IncomingEvent


class BuyBoxProjector:
    def __init__(self, repo: BuyBoxRepository) -> None:
        self._repo = repo

    async def on_offer_created(self, event: IncomingEvent) -> None:
        p = event.payload
        existing = await self._repo.get_offer(uuid.UUID(p["offer_id"]))
        if existing is None:
            offer = OfferProjection(
                offer_id=uuid.UUID(p["offer_id"]),
                variant_id=uuid.UUID(p["variant_id"]),
                seller_id=uuid.UUID(p["seller_id"]),
                region=event.region,
                offer_created_at=event.occurred_at,
                is_active=False,
                currency=p.get("currency"),
            )
        else:
            existing.offer_created_at = event.occurred_at
            existing.region = event.region
            offer = existing
        await self._repo.upsert_offer(offer)
        await self._recompute(offer, event)

    async def on_offer_activated(self, event: IncomingEvent) -> None:
        p = event.payload
        offer = await self._get_or_init(event)
        offer.is_active = True
        offer.currency = p.get("currency")
        offer.amount = p.get("price_amount")
        offer.scale = int(p.get("price_scale") or 0)
        await self._repo.upsert_offer(offer)
        await self._recompute(offer, event)

    async def on_offer_price_changed(self, event: IncomingEvent) -> None:
        p = event.payload
        offer = await self._get_or_init(event)
        offer.currency = p.get("currency")
        offer.amount = p.get("new_amount")
        offer.scale = int(p.get("scale") or 0)
        await self._repo.upsert_offer(offer)
        await self._recompute(offer, event)

    async def on_offer_suspended(self, event: IncomingEvent) -> None:
        offer = await self._get_or_init(event)
        # Price/currency are kept: a later OfferActivated makes the offer a
        # candidate again without needing the price event replayed.
        offer.is_active = False
        await self._repo.upsert_offer(offer)
        await self._recompute(offer, event)

    # -- helpers --
    async def _get_or_init(self, event: IncomingEvent) -> OfferProjection:
        """Same-partition ordering guarantees Created arrives first; this is a
        replay-safety fallback if an offer row is missing."""
        p = event.payload
        existing = await self._repo.get_offer(uuid.UUID(p["offer_id"]))
        if existing is not None:
            return existing
        return OfferProjection(
            offer_id=uuid.UUID(p["offer_id"]),
            variant_id=uuid.UUID(p["variant_id"]),
            seller_id=uuid.UUID(p["seller_id"]),
            region=event.region,
            offer_created_at=event.occurred_at,
        )

    async def _recompute(self, offer: OfferProjection, event: IncomingEvent) -> None:
        offers = await self._repo.list_for(offer.variant_id, offer.region)
        winner = pick_winner(offers)
        await self._repo.set_variant_state(
            offer.variant_id, offer.region, winner, last_updated_at=event.occurred_at
        )
