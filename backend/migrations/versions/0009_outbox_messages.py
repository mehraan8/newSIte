"""outbox_messages (transactional outbox)

Concrete outbox table written atomically with state changes by the Unit of Work.
event_id is the PK -> idempotency-safe storage (an event cannot be stored twice).

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "outbox_messages",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("aggregate_type", sa.String(120), nullable=False),
        sa.Column("event_type", sa.String(160), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("published_flag", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("retry_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )
    # Relay poll index: oldest unpublished first.
    op.create_index(
        "ix_outbox_unpublished",
        "outbox_messages",
        ["created_at"],
        postgresql_where=sa.text("published_flag = false"),
    )


def downgrade() -> None:
    op.drop_table("outbox_messages")
