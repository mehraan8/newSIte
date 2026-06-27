"""outbox_messages -> production envelope

Adds ADR-0008 envelope columns + relay dispatch-state to the outbox table from
0009: idempotency_key (unique), event_version, correlation/causation/tenant,
region, pii_refs, available_at, published_at, last_error. Replaces the simple
unpublished index with a due-time relay index and a correlation index.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
T = "outbox_messages"


def upgrade() -> None:
    # --- envelope columns (NN ones carry server defaults → safe on any rows) ---
    op.add_column(T, sa.Column("event_version", sa.SmallInteger, nullable=False, server_default="1"))
    op.add_column(T, sa.Column("correlation_id", postgresql.UUID(as_uuid=True)))
    op.add_column(T, sa.Column("causation_id", postgresql.UUID(as_uuid=True)))
    op.add_column(T, sa.Column("tenant_id", postgresql.UUID(as_uuid=True)))
    op.add_column(T, sa.Column("region", sa.String(20), nullable=False, server_default="global"))
    op.add_column(
        T, sa.Column("pii_refs", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))
    )

    # --- dispatch state ---
    op.add_column(T, sa.Column("available_at", TS, nullable=False, server_default=NOW))
    op.add_column(T, sa.Column("published_at", TS))
    op.add_column(T, sa.Column("last_error", sa.Text))

    # --- idempotency_key: add nullable, backfill from event_id, enforce NN + UNIQUE ---
    op.add_column(T, sa.Column("idempotency_key", sa.String(80)))
    op.execute(f"UPDATE {T} SET idempotency_key = event_id::text WHERE idempotency_key IS NULL")
    op.alter_column(T, "idempotency_key", nullable=False)
    op.create_unique_constraint("uq_outbox_idempotency", T, ["idempotency_key"])

    # --- indexes: replace simple-unpublished with due-time relay + correlation ---
    op.drop_index("ix_outbox_unpublished", table_name=T)
    op.create_index(
        "ix_outbox_ready", T, ["available_at"],
        postgresql_where=sa.text("published_flag = false"),
    )
    op.create_index("ix_outbox_correlation", T, ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_correlation", table_name=T)
    op.drop_index("ix_outbox_ready", table_name=T)
    op.create_index(
        "ix_outbox_unpublished", T, ["created_at"],
        postgresql_where=sa.text("published_flag = false"),
    )
    op.drop_constraint("uq_outbox_idempotency", T, type_="unique")
    for col in (
        "idempotency_key", "last_error", "published_at", "available_at",
        "pii_refs", "region", "tenant_id", "causation_id", "correlation_id", "event_version",
    ):
        op.drop_column(T, col)
