"""Read-only ACL to the BuyBox pricing read model.

The only place Ordering learns an offer's price. Narrow SQL against BuyBox's
per-offer projection — deliberately NOT an import of BuyBox's repository or ORM,
so there is no Python coupling to break when BuyBox changes and the adapter
becomes an HTTP call on extraction. Mirrors Inventory's SqlSellingGateway.

Opens its own session, outside any UoW: a cross-context read must never join the
write transaction.

Sourced from BuyBox rather than Selling's write tables on purpose — it is the
price the customer was actually shown, and it already carries `is_active`, so a
suspended offer cannot be ordered. The cost: BuyBox is eventually consistent, so
an offer activated seconds ago is briefly unorderable. That fails CLOSED (no
sale) rather than open (wrong price), which is the correct direction for a
pricing gateway.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.modules.ordering.application.ports import OfferPriceSnapshot
from src.shared_kernel.infrastructure.database import SessionFactory

_PRICE_SQL = text(
    """
    SELECT offer_id, region, currency, amount, scale, is_active
      FROM buy_box_offer
     WHERE offer_id = :offer_id
       AND region   = :region
     LIMIT 1
    """
)


class SqlBuyBoxPricingGateway:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession] = SessionFactory
    ) -> None:
        self._sf = session_factory

    async def price_for(
        self, offer_id: uuid.UUID, region: str
    ) -> OfferPriceSnapshot | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    _PRICE_SQL, {"offer_id": str(offer_id), "region": region}
                )
            ).first()
        if row is None:
            return None
        return OfferPriceSnapshot(
            offer_id=row.offer_id,
            region=row.region,
            currency=row.currency,
            amount=int(row.amount) if row.amount is not None else None,
            scale=int(row.scale),
            is_active=bool(row.is_active),
        )
