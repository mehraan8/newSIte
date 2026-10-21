"""selling: offers + offer_prices

First financial tables. No cross-context FK on variant_id/seller_id (isolation).
DB backstops:
  * uq_offer_active     — a seller cannot have two ACTIVE offers for the same
                          (variant, condition)  [hard dedup]
  * uq_offer_open_price — at most one OPEN price window per offer (valid_to NULL)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "offers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("seller_id", UUID, nullable=False),          # ref only, no FK
        sa.Column("variant_id", UUID, nullable=False),         # ref only, no FK
        sa.Column("condition", sa.String(20), nullable=False, server_default="new"),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint("condition IN ('new','refurbished','used')", name="ck_offer_condition"),
        sa.CheckConstraint("status IN ('draft','active','suspended')", name="ck_offer_status"),
    )
    op.create_index("ix_offers_variant", "offers", ["variant_id"])
    op.create_index("ix_offers_seller", "offers", ["seller_id"])
    op.create_index(
        "uq_offer_active", "offers", ["seller_id", "variant_id", "condition"], unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "offer_prices",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("offer_id", UUID, sa.ForeignKey("offers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Numeric(20, 0), nullable=False),         # minor units
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("scale", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("compare_at_amount", sa.Numeric(20, 0)),
        sa.Column("valid_from", TS, nullable=False, server_default=NOW),
        sa.Column("valid_to", TS),
        sa.CheckConstraint("amount >= 0", name="ck_offer_price_nonneg"),
    )
    op.create_index("ix_offer_prices_offer", "offer_prices", ["offer_id"])
    op.create_index(
        "uq_offer_open_price", "offer_prices", ["offer_id"], unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("offer_prices")
    op.drop_table("offers")
