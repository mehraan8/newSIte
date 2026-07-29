"""ordering: optimistic lock on the saga aggregate

orders_saga had no version column, so two saga reactions for different lines of
the same order could read the same state and the second writer would silently
overwrite the first's line status. It worked only because the relay is pinned to
one replica — making a deployment topology load-bearing for correctness.

The version guards the aggregate ROOT. Line rows are part of the same aggregate,
so a writer that wins the root CAS is the only one mutating its lines.

Existing rows get version 0, which is correct: any in-flight reader holding a
pre-migration Order also assumes 0.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders_saga",
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("orders_saga", "version")
