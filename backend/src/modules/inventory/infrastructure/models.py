"""Inventory ORM (migration 0014). Offer-anchored, no cross-context FK.

CHECK constraints are the last-line oversell backstop; `version` powers optimistic
locking. (Supersedes the unused variant-anchored tables from migration 0006.)
"""

from __future__ import annotations

from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared_kernel.infrastructure.database import Base


class InventoryStockModel(Base):
    __tablename__ = "inventory_stock"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    location: Mapped[str] = mapped_column(String(40), nullable=False)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("offer_id", "location", name="uq_inventory_offer_location"),
        CheckConstraint("on_hand >= 0", name="ck_inv_onhand_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_inv_reserved_nonneg"),
        CheckConstraint("reserved <= on_hand", name="ck_inv_reserved_le_onhand"),
    )


class ReservationModel(Base):
    __tablename__ = "inventory_reservation"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    location: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'pending'"))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_resv_qty_pos"),
        CheckConstraint(
            "status IN ('pending','committed','released','expired')", name="ck_resv_status"
        ),
        # Sweeper index: only still-pending reservations that can expire.
        Index("ix_resv_pending_expiry", "expires_at", postgresql_where=text("status = 'pending'")),
        # I8 reconciliation index (migration 0016). Declared here too, or
        # metadata.create_all() builds a schema that differs from the migrated
        # one and autogenerate proposes dropping it.
        Index("ix_resv_offer_location_status", "offer_id", "location", "status"),
    )
