"""buybox read model: buy_box_offer + buy_box_variant_state

Projection tables, fully rebuildable from events. No FK to Selling/Catalog.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "buy_box_offer",
        sa.Column("offer_id", UUID, primary_key=True),
        sa.Column("variant_id", UUID, nullable=False),
        sa.Column("seller_id", UUID, nullable=False),
        sa.Column("region", sa.String(20), nullable=False),
        sa.Column("currency", sa.CHAR(3)),
        sa.Column("amount", sa.Numeric(20, 0)),
        sa.Column("scale", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("offer_created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )
    # Recompute scan key.
    op.create_index("ix_buybox_offer_variant_region", "buy_box_offer", ["variant_id", "region"])

    op.create_table(
        "buy_box_variant_state",
        sa.Column("variant_id", UUID, nullable=False),
        sa.Column("region", sa.String(20), nullable=False),
        sa.Column("winning_offer_id", UUID),
        sa.Column("seller_id", UUID),
        sa.Column("amount", sa.Numeric(20, 0)),
        sa.Column("currency", sa.CHAR(3)),
        sa.Column("scale", sa.SmallInteger),
        sa.Column("last_updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("variant_id", "region", name="pk_buy_box_variant_state"),
    )


def downgrade() -> None:
    op.drop_table("buy_box_variant_state")
    op.drop_table("buy_box_offer")
