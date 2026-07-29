"""inventory (offer-anchored): inventory_stock + inventory_reservation

First hard-consistency tables. Optimistic locking via `version`; CHECK
constraints are the oversell backstop. No cross-context FK (isolation).
Supersedes the unused variant-anchored tables from 0006.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "inventory_stock",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("offer_id", UUID, nullable=False),
        sa.Column("location", sa.String(40), nullable=False),
        sa.Column("on_hand", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("offer_id", "location", name="uq_inventory_offer_location"),
        sa.CheckConstraint("on_hand >= 0", name="ck_inv_onhand_nonneg"),
        sa.CheckConstraint("reserved >= 0", name="ck_inv_reserved_nonneg"),
        sa.CheckConstraint("reserved <= on_hand", name="ck_inv_reserved_le_onhand"),
    )

    op.create_table(
        "inventory_reservation",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("offer_id", UUID, nullable=False),
        sa.Column("location", sa.String(40), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("expires_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint("quantity > 0", name="ck_resv_qty_pos"),
        sa.CheckConstraint(
            "status IN ('pending','committed','released','expired')", name="ck_resv_status"
        ),
    )
    op.create_index(
        "ix_resv_pending_expiry", "inventory_reservation", ["expires_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("inventory_reservation")
    op.drop_table("inventory_stock")
