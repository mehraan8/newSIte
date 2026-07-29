"""SQLAlchemy adapter for the Offer aggregate.

Returns ONLY domain Offers. Price history is append-only: the current price is
the open row (valid_to IS NULL); change_price closes the old window and opens a
new one. Explicit queries only — no lazy IO. Never commits.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.selling.domain.entities import Offer, OfferStatus
from src.modules.selling.infrastructure.models import OfferModel, OfferPriceModel
from src.shared_kernel.application.unit_of_work import Tracker
from src.shared_kernel.domain.base import utcnow
from src.shared_kernel.domain.value_objects import Money


class SqlOfferRepository:
    def __init__(self, session: AsyncSession, tracker: Tracker | None = None) -> None:
        self._s = session
        self._tracker = tracker

    def _track(self, offer: Offer) -> None:
        if self._tracker is not None:
            self._tracker.track(offer)

    async def _open_price(self, offer_id: uuid.UUID) -> OfferPriceModel | None:
        return await self._s.scalar(
            select(OfferPriceModel).where(
                OfferPriceModel.offer_id == offer_id,
                OfferPriceModel.valid_to.is_(None),
            )
        )

    def _to_domain(self, row: OfferModel, price: OfferPriceModel | None) -> Offer:
        current = compare = None
        if price is not None:
            current = Money(amount=int(price.amount), currency=price.currency, scale=price.scale)
            if price.compare_at_amount is not None:
                compare = Money(
                    amount=int(price.compare_at_amount), currency=price.currency, scale=price.scale
                )
        return Offer(
            id=row.id,
            seller_id=row.seller_id,
            variant_id=row.variant_id,
            condition=row.condition,
            currency=row.currency,
            status=row.status,
            current_price=current,
            compare_at=compare,
        )

    async def get_by_id(self, offer_id: uuid.UUID) -> Offer | None:
        row = await self._s.get(OfferModel, offer_id)
        if row is None:
            return None
        offer = self._to_domain(row, await self._open_price(offer_id))
        self._track(offer)
        return offer

    async def find_active_for(self, seller_id: uuid.UUID, variant_id: uuid.UUID) -> Offer | None:
        row = await self._s.scalar(
            select(OfferModel).where(
                OfferModel.seller_id == seller_id,
                OfferModel.variant_id == variant_id,
                OfferModel.status == OfferStatus.ACTIVE,
            )
        )
        if row is None:
            return None
        offer = self._to_domain(row, await self._open_price(row.id))
        self._track(offer)
        return offer

    async def add(self, offer: Offer) -> None:
        self._s.add(
            OfferModel(
                id=offer.id,
                seller_id=offer.seller_id,
                variant_id=offer.variant_id,
                condition=offer.condition,
                currency=offer.currency,
                status=offer.status,
            )
        )
        if offer.current_price is not None:
            self._s.add(self._new_price_row(offer))
        await self._s.flush()
        self._track(offer)

    async def save(self, offer: Offer) -> None:
        row = await self._s.get(OfferModel, offer.id)
        if row is None:
            raise LookupError(f"offer {offer.id} not found for update")
        row.status = offer.status
        row.condition = offer.condition
        row.currency = offer.currency
        row.updated_at = utcnow()

        # Reconcile price window: open a new row only if the current price differs.
        open_row = await self._open_price(offer.id)
        if self._price_differs(open_row, offer):
            if open_row is not None:
                open_row.valid_to = utcnow()        # close the old window
            if offer.current_price is not None:
                self._s.add(self._new_price_row(offer))

        await self._s.flush()
        self._track(offer)

    # -- helpers --
    def _new_price_row(self, offer: Offer) -> OfferPriceModel:
        p = offer.current_price
        return OfferPriceModel(
            offer_id=offer.id,
            amount=p.amount,
            currency=p.currency,
            scale=p.scale,
            compare_at_amount=(offer.compare_at.amount if offer.compare_at else None),
        )

    @staticmethod
    def _price_differs(open_row: OfferPriceModel | None, offer: Offer) -> bool:
        cur = offer.current_price
        if open_row is None:
            return cur is not None
        if cur is None:
            return True
        compare = offer.compare_at.amount if offer.compare_at else None
        return not (
            int(open_row.amount) == cur.amount
            and open_row.currency == cur.currency
            and open_row.scale == cur.scale
            and (int(open_row.compare_at_amount) if open_row.compare_at_amount is not None else None)
            == compare
        )
