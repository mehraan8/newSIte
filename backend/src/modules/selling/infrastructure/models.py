"""ORM models for the Selling context (offers, offer_prices — migration 0012).

variant_id and seller_id are plain UUID columns with NO cross-context FK — the
Selling schema must not couple to Catalog/Identity tables (extractability).
"""

from __future__ import annotations

from typing import Optional

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Numeric

from src.shared_kernel.infrastructure.database import Base


class OfferModel(Base):
    __tablename__ = "offers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    condition: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'new'"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class OfferPriceModel(Base):
    __tablename__ = "offer_prices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 0), nullable=False)  # minor units
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    scale: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    compare_at_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 0))
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
