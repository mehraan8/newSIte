"""inventory

warehouses and inventory_items. inventory_reservations references orders and is
therefore created in the ordering migration (0006).

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "warehouses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("province", sa.String(50)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("code", name="uq_warehouse_code"),
    )

    op.create_table(
        "inventory_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", sa.BigInteger, nullable=False),
        sa.Column("quantity_on_hand", sa.Integer, nullable=False, server_default="0"),
        sa.Column("quantity_reserved", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reorder_level", sa.Integer, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("variant_id", "warehouse_id", name="uq_inv_variant_wh"),
        sa.CheckConstraint("quantity_on_hand >= 0", name="ck_inv_onhand_nonneg"),
        sa.CheckConstraint("quantity_reserved >= 0", name="ck_inv_reserved_nonneg"),
        sa.CheckConstraint(
            "quantity_reserved <= quantity_on_hand", name="ck_inv_reserved_le_onhand",
        ),
    )
    op.create_index("ix_inv_variant", "inventory_items", ["variant_id"])


def downgrade() -> None:
    op.drop_table("inventory_items")
    op.drop_table("warehouses")
