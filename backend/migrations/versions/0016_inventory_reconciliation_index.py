"""inventory: reconciliation index on inventory_reservation

Supports ADR-0009 invariant I8:

    SUM(quantity) WHERE status='pending' AND (offer_id, location) = X
      ==  inventory_stock.reserved  for X

I8 is the seam created by keeping Reservation outside the InventoryItem
aggregate, so it is auditable rather than structurally enforced. The audit query
had no supporting index and degraded to a sequential scan as reservation history
accumulated.

No schema change is needed for inventory.InventoryExpired: `ck_resv_status`
already admits 'expired', and events are rows in the existing outbox.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_resv_offer_location_status",
        "inventory_reservation",
        ["offer_id", "location", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_resv_offer_location_status", table_name="inventory_reservation")
