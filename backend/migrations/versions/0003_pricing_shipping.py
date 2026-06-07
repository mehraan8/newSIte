"""pricing & shipping

prices (with no-overlap EXCLUDE window), discounts, discount_targets,
shipping_zones, shipping_methods.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
MONEY = sa.Numeric(18, 0)


def upgrade() -> None:
    # --- prices -----------------------------------------------------------
    op.create_table(
        "prices",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default="IRR"),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("compare_at_amount", MONEY),
        sa.Column("valid_from", TS, nullable=False, server_default=NOW),
        sa.Column("valid_to", TS),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("amount >= 0", name="ck_price_amount_nonneg"),
        sa.CheckConstraint(
            "compare_at_amount IS NULL OR compare_at_amount >= amount",
            name="ck_price_compare_at",
        ),
    )
    op.create_index("ix_price_variant_valid", "prices", ["variant_id", sa.text("valid_from DESC")])
    # No two active price windows may overlap for the same variant.
    op.execute(
        """
        ALTER TABLE prices ADD CONSTRAINT ex_price_no_overlap
        EXCLUDE USING gist (
            variant_id WITH =,
            tstzrange(valid_from, COALESCE(valid_to, 'infinity'::timestamptz)) WITH &&
        )
        """
    )

    # --- discounts --------------------------------------------------------
    op.create_table(
        "discounts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("discount_type", sa.String(10), nullable=False),
        sa.Column("value", sa.Numeric(18, 4), nullable=False),
        sa.Column("scope", sa.String(15), nullable=False),
        sa.Column("min_order_amount", MONEY),
        sa.Column("max_uses", sa.Integer),
        sa.Column("used_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("per_user_limit", sa.Integer),
        sa.Column("starts_at", TS, nullable=False, server_default=NOW),
        sa.Column("ends_at", TS),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("code", name="uq_discount_code"),
        sa.CheckConstraint("discount_type IN ('percent','fixed')", name="ck_discount_type"),
        sa.CheckConstraint("scope IN ('cart','product','category')", name="ck_discount_scope"),
        sa.CheckConstraint("value > 0", name="ck_discount_value_pos"),
        sa.CheckConstraint(
            "discount_type <> 'percent' OR value <= 100", name="ck_discount_percent_range",
        ),
    )
    op.create_index(
        "ix_discount_active", "discounts", ["is_active"],
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "discount_targets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("discount_id", sa.BigInteger, nullable=False),
        sa.Column("target_type", sa.String(10), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["discount_id"], ["discounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("discount_id", "target_type", "target_id", name="uq_disc_target"),
        sa.CheckConstraint("target_type IN ('product','category')", name="ck_disc_target_type"),
    )

    # --- shipping_zones / shipping_methods --------------------------------
    op.create_table(
        "shipping_zones",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("provinces", postgresql.ARRAY(sa.String(50)), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "shipping_methods",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("zone_id", sa.BigInteger, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("carrier", sa.String(60)),
        sa.Column("base_cost", MONEY, nullable=False),
        sa.Column("per_kg_cost", MONEY, nullable=False, server_default="0"),
        sa.Column("estimated_days", sa.SmallInteger),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["zone_id"], ["shipping_zones.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("base_cost >= 0", name="ck_ship_base_nonneg"),
    )
    op.create_index("ix_ship_zone", "shipping_methods", ["zone_id"])


def downgrade() -> None:
    op.drop_table("shipping_methods")
    op.drop_table("shipping_zones")
    op.drop_table("discount_targets")
    op.drop_table("discounts")
    op.execute("ALTER TABLE prices DROP CONSTRAINT IF EXISTS ex_price_no_overlap")
    op.drop_table("prices")
