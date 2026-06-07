"""ordering

orders (state machine + snapshots), order_lines, order_status_history, and
inventory_reservations (placed here because it references orders).

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
MONEY = sa.Numeric(18, 0)
UUID_DEFAULT = sa.text("gen_random_uuid()")

ORDER_STATES = (
    "pending_payment", "paid", "processing", "shipped",
    "delivered", "cancelled", "refunded", "failed",
)


def upgrade() -> None:
    # --- orders (aggregate root) -----------------------------------------
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("order_number", sa.String(20), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending_payment"),
        sa.Column("subtotal", MONEY, nullable=False),
        sa.Column("discount_total", MONEY, nullable=False, server_default="0"),
        sa.Column("shipping_total", MONEY, nullable=False, server_default="0"),
        sa.Column("tax_total", MONEY, nullable=False, server_default="0"),
        sa.Column("grand_total", MONEY, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default="IRR"),
        sa.Column("shipping_address_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("shipping_method_id", sa.BigInteger),
        sa.Column("applied_discount_id", sa.BigInteger),
        sa.Column("placed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["shipping_method_id"], ["shipping_methods.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["applied_discount_id"], ["discounts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("order_number", name="uq_order_number"),
        sa.CheckConstraint(
            "status IN ('" + "','".join(ORDER_STATES) + "')", name="ck_order_status",
        ),
        sa.CheckConstraint(
            "grand_total = subtotal - discount_total + shipping_total + tax_total",
            name="ck_order_total",
        ),
    )
    op.create_index("ix_order_user_placed", "orders", ["user_id", sa.text("placed_at DESC")])
    op.create_index("ix_order_status", "orders", ["status"])

    # --- order_lines (immutable snapshots) -------------------------------
    op.create_table(
        "order_lines",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True)),
        sa.Column("product_name_snapshot", sa.String(255), nullable=False),
        sa.Column("sku_snapshot", sa.String(64), nullable=False),
        sa.Column("attributes_snapshot", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("unit_price", MONEY, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("line_total", MONEY, nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="SET NULL"),
        sa.CheckConstraint("quantity > 0", name="ck_orderline_qty_pos"),
        sa.CheckConstraint("line_total = unit_price * quantity", name="ck_orderline_total"),
    )
    op.create_index("ix_orderline_order", "order_lines", ["order_id"])

    # --- order_status_history (audit; partition by month later) ----------
    op.create_table(
        "order_status_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(20)),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(255)),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_osh_order_created", "order_status_history", ["order_id", "created_at"])

    # --- inventory_reservations (references orders) ----------------------
    op.create_table(
        "inventory_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="held"),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.CheckConstraint("quantity > 0", name="ck_resv_qty_pos"),
        sa.CheckConstraint("status IN ('held','committed','released')", name="ck_resv_status"),
    )
    op.create_index("ix_resv_order", "inventory_reservations", ["order_id"])
    op.create_index("ix_resv_variant", "inventory_reservations", ["variant_id"])
    # Sweeper index: only the still-held rows that can expire.
    op.create_index(
        "ix_resv_expires", "inventory_reservations", ["expires_at"],
        postgresql_where=sa.text("status = 'held'"),
    )


def downgrade() -> None:
    op.drop_table("inventory_reservations")
    op.drop_table("order_status_history")
    op.drop_table("order_lines")
    op.drop_table("orders")
