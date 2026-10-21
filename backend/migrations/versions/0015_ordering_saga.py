"""ordering: orders_saga + order_saga_lines

Saga aggregate tables. order_saga_lines.reservation_id is unique (the saga maps
inventory replies back to the order via it). No cross-context FK.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "orders_saga",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("customer_id", UUID, nullable=False),
        sa.Column("region", sa.String(20), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("total_amount", sa.BigInteger, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('pending','reserved','paid','failed','cancelled')", name="ck_order_status"
        ),
    )
    op.create_index("ix_orders_saga_customer", "orders_saga", ["customer_id"])

    op.create_table(
        "order_saga_lines",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", UUID, sa.ForeignKey("orders_saga.id", ondelete="CASCADE"), nullable=False),
        sa.Column("offer_id", UUID, nullable=False),
        sa.Column("location", sa.String(40), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_amount", sa.BigInteger, nullable=False),
        sa.Column("reservation_id", UUID, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.UniqueConstraint("reservation_id", name="uq_order_line_reservation"),
    )
    op.create_index("ix_order_saga_lines_order", "order_saga_lines", ["order_id"])


def downgrade() -> None:
    op.drop_table("order_saga_lines")
    op.drop_table("orders_saga")
