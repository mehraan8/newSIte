"""ordering: Idempotency-Key dedup for POST /orders

Checkout was not idempotent: a retried request (network timeout, double-tap,
mobile auto-retry) created a SECOND order with fresh reservation_ids — double
stock hold and, once Payments is real, a double charge.

Dedup is a unique (customer_id, idempotency_key) whose row is written in the SAME
transaction as the order, so there is no window in which an order exists without
its key recorded. Scoped by customer so one caller's key cannot collide with — or
be used to probe — another's.

`request_fingerprint` stores a hash of the canonicalised request. Replaying a key
with a DIFFERENT body is a client bug; returning the original order silently
would hide it, so it is rejected instead.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "order_idempotency",
        sa.Column("customer_id", UUID, primary_key=True),
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column(
            "order_id",
            UUID,
            sa.ForeignKey("orders_saga.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("order_idempotency")
