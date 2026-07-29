"""BuyBox read endpoint — the winning offer per variant per region."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.buybox.infrastructure.repository import SqlBuyBoxRepository
from src.shared_kernel.application.errors import NotFoundError
from src.shared_kernel.infrastructure.database import get_session

router = APIRouter(prefix="/buybox", tags=["buybox"])


class BuyBoxResponse(BaseModel):
    variant_id: str
    region: str
    winning_offer_id: str | None
    seller_id: str | None
    amount: int | None
    currency: str | None
    scale: int | None


@router.get("/variants/{variant_id}", response_model=BuyBoxResponse)
async def get_buybox(
    variant_id: uuid.UUID,
    region: str = Query(default="global"),
    session: AsyncSession = Depends(get_session),
) -> BuyBoxResponse:
    state = await SqlBuyBoxRepository(session).get_variant_state(variant_id, region)
    if state is None or state.winning_offer_id is None:
        raise NotFoundError("آفر برندهٔ این محصول یافت نشد.")
    return BuyBoxResponse(
        variant_id=str(state.variant_id),
        region=state.region,
        winning_offer_id=str(state.winning_offer_id),
        seller_id=str(state.seller_id) if state.seller_id else None,
        amount=state.amount,
        currency=state.currency,
        scale=state.scale,
    )
