"""payment

payments (idempotent, server-verified) and payment_transactions (append-only
gateway audit log).

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
MONEY = sa.Numeric(18, 0)
UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gateway", sa.String(30), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default="IRR"),
        sa.Column("status", sa.String(15), nullable=False, server_default="initiated"),
        sa.Column("authority", sa.String(100)),
        sa.Column("ref_id", sa.String(100)),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name="uq_pay_idem"),
        sa.CheckConstraint("amount > 0", name="ck_pay_amount_pos"),
        sa.CheckConstraint(
            "status IN ('initiated','pending','succeeded','failed','refunded')",
            name="ck_pay_status",
        ),
    )
    op.create_index("ix_pay_order", "payments", ["order_id"])
    op.create_index(
        "uq_pay_authority", "payments", ["authority"], unique=True,
        postgresql_where=sa.text("authority IS NOT NULL"),
    )

    op.create_table(
        "payment_transactions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("txn_type", sa.String(10), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("request_payload", postgresql.JSONB),
        sa.Column("response_payload", postgresql.JSONB),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.CheckConstraint("txn_type IN ('init','verify','refund')", name="ck_paytxn_type"),
    )
    op.create_index("ix_paytxn_payment", "payment_transactions", ["payment_id"])


def downgrade() -> None:
    op.drop_table("payment_transactions")
    op.drop_table("payments")
