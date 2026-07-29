"""BuyBox projection ports (DB-free)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from src.modules.buybox.application.ranking import OfferProjection


@dataclass(frozen=True)
class VariantState:
    variant_id: uuid.UUID
    region: str
    winning_offer_id: uuid.UUID | None
    seller_id: uuid.UUID | None
    amount: int | None
    currency: str | None
    scale: int | None
    last_updated_at: datetime


class BuyBoxRepository(Protocol):
    async def get_offer(self, offer_id: uuid.UUID) -> OfferProjection | None: ...
    async def upsert_offer(self, offer: OfferProjection) -> None: ...
    async def list_for(self, variant_id: uuid.UUID, region: str) -> list[OfferProjection]: ...
    async def set_variant_state(
        self,
        variant_id: uuid.UUID,
        region: str,
        winner: OfferProjection | None,
        last_updated_at: datetime,
    ) -> None: ...
    async def get_variant_state(
        self, variant_id: uuid.UUID, region: str
    ) -> VariantState | None: ...
