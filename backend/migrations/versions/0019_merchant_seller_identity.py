"""merchant: sellers + seller_members (the authoritative Seller identity)

Schema foundation ONLY (ADR-0011 §7, Phase 1C M1). No data is written by this
migration, by design:

  * NO backfill from offers.seller_id. A read-only audit found that of 5 distinct
    values only 2 resolved to a user and 3 were arbitrary UUIDs accepted by the
    pre-D4 endpoint, so those values are not seller identities and must not
    become Seller.id (ADR-0011 §7.2). The disposable residue was removed by a
    separate operational cleanup, not by this migration.
  * NO Seller.id derived from a user id, ever.
  * NO RBAC seeding — permissions/roles/role_permissions belong to Phase 1E.

Isolation:
  * seller_members.seller_id -> sellers.id IS enforced; both tables are Merchant's.
  * seller_members.user_id has NO FK to users. Merchant must not couple
    relationally to Identity, the same rule migration 0012 applies to
    offers.seller_id / offers.variant_id.
  * NO FK is added to offers.seller_id, buy_box_offer.seller_id or (future)
    inventory_stock.seller_id. Those stay local references/snapshots owned by
    their contexts (ADR-0011 §9).

DB backstops:
  * pk_seller_members (seller_id, user_id) — a user cannot hold two memberships
    in one seller; enforced by the database, not only by application code.
  * ck_seller_status / ck_seller_member_status / ck_seller_member_role — the
    domain vocabularies, mirrored as CHECK constraints like ck_offer_status.
  * ix_seller_members_user — the Phase 1F membership lookup path (user -> sellers).

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "sellers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("tenant_id", UUID),                       # ref only, no FK
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('pending','active','suspended')", name="ck_seller_status"
        ),
        sa.UniqueConstraint("code", name="uq_sellers_code"),
    )
    op.create_index("ix_sellers_tenant", "sellers", ["tenant_id"])

    op.create_table(
        "seller_members",
        sa.Column(
            "seller_id", UUID,
            sa.ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("user_id", UUID, nullable=False),         # ref only, no FK
        sa.Column("member_role_code", sa.String(32), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("seller_id", "user_id", name="pk_seller_members"),
        sa.CheckConstraint(
            "status IN ('active','suspended')", name="ck_seller_member_status"
        ),
        sa.CheckConstraint(
            "member_role_code IN "
            "('seller_owner','seller_manager','seller_staff','seller_viewer')",
            name="ck_seller_member_role",
        ),
    )
    # user -> sellers: the direction Phase 1F resolves on every seller-scoped
    # request. The composite PK already serves seller -> users.
    op.create_index("ix_seller_members_user", "seller_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_seller_members_user", table_name="seller_members")
    op.drop_table("seller_members")
    op.drop_index("ix_sellers_tenant", table_name="sellers")
    op.drop_table("sellers")
