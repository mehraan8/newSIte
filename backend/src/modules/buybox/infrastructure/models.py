"""BuyBox read-model tables (migration 0013).

Both are projection-owned and fully rebuildable from events. No FK to Selling
or Catalog — they reference by id only.
"""

from __future__ import annotations

from typing import Optional

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared_kernel.infrastructure.database import Base


class BuyBoxOfferModel(Base):
    """Derived per-offer state — enough to recompute the winner from events."""

    __tablename__ = "buy_box_offer"

    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    region: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[Optional[str]] = mapped_column(String(3))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 0))
    scale: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    offer_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BuyBoxVariantStateModel(Base):
    """The projection: winning offer per (variant_id, region)."""

    __tablename__ = "buy_box_variant_state"

    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    region: Mapped[str] = mapped_column(String(20), primary_key=True)
    winning_offer_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    seller_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 0))
    currency: Mapped[Optional[str]] = mapped_column(String(3))
    scale: Mapped[Optional[int]] = mapped_column(SmallInteger)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
