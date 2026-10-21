"""processed_events (inbox) + dead_letter_messages (DLQ)

Consumer-side dedup table and the dead-letter parking table for poison events.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def upgrade() -> None:
    # --- consumer inbox: (handler, idempotency_key) processed-once ---
    op.create_table(
        "processed_events",
        sa.Column("handler_name", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(160), nullable=False),
        sa.Column("processed_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("handler_name", "idempotency_key", name="pk_processed_events"),
    )

    # --- dead-letter queue (no broker) ---
    op.create_table(
        "dead_letter_messages",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("aggregate_type", sa.String(120), nullable=False),
        sa.Column("event_type", sa.String(160), nullable=False),
        sa.Column("event_version", sa.SmallInteger, nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True)),
        sa.Column("region", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("attempts", sa.SmallInteger, nullable=False),
        sa.Column("dead_lettered_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_dlq_event_type", "dead_letter_messages", ["event_type"])


def downgrade() -> None:
    op.drop_table("dead_letter_messages")
    op.drop_table("processed_events")
