"""cart

carts (user or guest) and cart_items with price snapshots.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
MONEY = sa.Numeric(18, 0)
UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "carts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("anonymous_token", sa.String(64)),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default="IRR"),
        sa.Column("expires_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('active','converted','abandoned')", name="ck_cart_status"),
        # Exactly one owner: a logged-in user OR a guest token, never both/neither.
        sa.CheckConstraint(
            "num_nonnulls(user_id, anonymous_token) = 1", name="ck_cart_owner",
        ),
    )
    op.create_index(
        "ix_cart_user", "carts", ["user_id"],
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_cart_anon", "carts", ["anonymous_token"], unique=True,
        postgresql_where=sa.text("anonymous_token IS NOT NULL"),
    )

    op.create_table(
        "cart_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("cart_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_price_snapshot", MONEY, nullable=False),
        sa.Column("added_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("cart_id", "variant_id", name="uq_cartitem_cart_variant"),
        sa.CheckConstraint("quantity > 0", name="ck_cartitem_qty_pos"),
    )
    op.create_index("ix_cartitem_cart", "cart_items", ["cart_id"])


def downgrade() -> None:
    op.drop_table("cart_items")
    op.drop_table("carts")
