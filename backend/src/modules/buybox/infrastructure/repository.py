"""SQLAlchemy implementation of BuyBoxRepository. Operates on the caller's
session (the relay tx) — never commits."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.buybox.application.ports import VariantState
from src.modules.buybox.application.ranking import OfferProjection
from src.modules.buybox.infrastructure.models import (
    BuyBoxOfferModel,
    BuyBoxVariantStateModel,
)
from src.shared_kernel.domain.base import utcnow


def _offer_to_domain(row: BuyBoxOfferModel) -> OfferProjection:
    return OfferProjection(
        offer_id=row.offer_id,
        variant_id=row.variant_id,
        seller_id=row.seller_id,
        region=row.region,
        offer_created_at=row.offer_created_at,
        is_active=row.is_active,
        currency=row.currency,
        amount=int(row.amount) if row.amount is not None else None,
        scale=row.scale,
    )


class SqlBuyBoxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_offer(self, offer_id: uuid.UUID) -> OfferProjection | None:
        row = await self._s.get(BuyBoxOfferModel, offer_id)
        return _offer_to_domain(row) if row else None

    async def upsert_offer(self, offer: OfferProjection) -> None:
        row = await self._s.get(BuyBoxOfferModel, offer.offer_id)
        if row is None:
            row = BuyBoxOfferModel(offer_id=offer.offer_id)
            self._s.add(row)
        row.variant_id = offer.variant_id
        row.seller_id = offer.seller_id
        row.region = offer.region
        row.currency = offer.currency
        row.amount = offer.amount
        row.scale = offer.scale
        row.is_active = offer.is_active
        row.offer_created_at = offer.offer_created_at
        row.updated_at = utcnow()
        await self._s.flush()

    async def list_for(self, variant_id: uuid.UUID, region: str) -> list[OfferProjection]:
        rows = await self._s.scalars(
            select(BuyBoxOfferModel).where(
                BuyBoxOfferModel.variant_id == variant_id,
                BuyBoxOfferModel.region == region,
            )
        )
        return [_offer_to_domain(r) for r in rows.all()]

    async def set_variant_state(
        self,
        variant_id: uuid.UUID,
        region: str,
        winner: OfferProjection | None,
        last_updated_at: datetime,
    ) -> None:
        row = await self._s.get(BuyBoxVariantStateModel, (variant_id, region))
        if row is None:
            row = BuyBoxVariantStateModel(variant_id=variant_id, region=region)
            self._s.add(row)
        if winner is None:
            row.winning_offer_id = None
            row.seller_id = None
            row.amount = None
            row.currency = None
            row.scale = None
        else:
            row.winning_offer_id = winner.offer_id
            row.seller_id = winner.seller_id
            row.amount = winner.amount
            row.currency = winner.currency
            row.scale = winner.scale
        row.last_updated_at = last_updated_at
        await self._s.flush()

    async def get_variant_state(
        self, variant_id: uuid.UUID, region: str
    ) -> VariantState | None:
        row = await self._s.get(BuyBoxVariantStateModel, (variant_id, region))
        if row is None:
            return None
        return VariantState(
            variant_id=row.variant_id,
            region=row.region,
            winning_offer_id=row.winning_offer_id,
            seller_id=row.seller_id,
            amount=int(row.amount) if row.amount is not None else None,
            currency=row.currency,
            scale=row.scale,
            last_updated_at=row.last_updated_at,
        )
