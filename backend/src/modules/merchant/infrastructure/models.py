"""ORM models for the Merchant context (migration 0019).

`seller_members.user_id` is a plain UUID with NO foreign key to `users`. That is
the same isolation rule Selling applies to `offers.seller_id` and
`offers.variant_id` (migration 0012): Merchant must not couple relationally to
Identity's schema, or neither can be extracted. Referential integrity INSIDE
Merchant (member -> seller) is enforced, because both tables belong here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared_kernel.infrastructure.database import Base


class SellerModel(Base):
    __tablename__ = "sellers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'pending'")
    )
    # Nullable: the system is multi-tenant READY, not multi-tenant. There is no
    # tenants table and no users.tenant_id, so there is nothing to reference yet
    # (ADR-0011 §8). No FK for the same reason.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Monotonic. Published in seller events so the Phase 1F authorization cache
    # can discard out-of-order deliveries (ADR-0011 §10.2).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SellerMemberModel(Base):
    __tablename__ = "seller_members"

    # Composite PK == the uniqueness invariant: a user cannot hold two
    # memberships in one seller, enforced by the database rather than by an
    # application check (ADR-0011 §13). Mirrors order_idempotency's composite key.
    seller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sellers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Deliberately NO ForeignKey to users.id — cross-context isolation.
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    member_role_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
