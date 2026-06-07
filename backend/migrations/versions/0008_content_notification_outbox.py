"""content/seo, notification & outbox

cms_pages, seo_metadata, redirects, notifications, and the transactional
outbox_events table that backs all cross-context async messaging.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    # --- Content / SEO ----------------------------------------------------
    op.create_table(
        "cms_pages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text),
        sa.Column("is_published", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("published_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("slug", name="uq_cms_slug"),
    )

    op.create_table(
        "seo_metadata",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("meta_title", sa.String(255)),
        sa.Column("meta_description", sa.String(500)),
        sa.Column("canonical_url", sa.String(500)),
        sa.Column("og_data", postgresql.JSONB),
        sa.Column("structured_data", postgresql.JSONB),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        # Polymorphic association is enforced in the application layer (no FK)
        # to keep Content decoupled from the entities it annotates.
        sa.UniqueConstraint("entity_type", "entity_id", name="uq_seo_entity"),
        sa.CheckConstraint(
            "entity_type IN ('product','category','page','brand')", name="ck_seo_entity_type",
        ),
    )

    op.create_table(
        "redirects",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("from_path", sa.String(500), nullable=False),
        sa.Column("to_path", sa.String(500), nullable=False),
        sa.Column("status_code", sa.SmallInteger, nullable=False, server_default="301"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("from_path", name="uq_redirect_from"),
        sa.CheckConstraint("status_code IN (301,302)", name="ck_redirect_status"),
    )

    # --- Notification -----------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("template", sa.String(60), nullable=False),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("sent_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("channel IN ('sms','email','push')", name="ck_notif_channel"),
        sa.CheckConstraint("status IN ('queued','sent','failed')", name="ck_notif_status"),
    )
    op.create_index("ix_notif_user", "notifications", ["user_id"])
    op.create_index("ix_notif_status", "notifications", ["status"])

    # --- Transactional outbox (the async backbone) -----------------------
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("aggregate_type", sa.String(50), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("processed_at", TS),
        sa.CheckConstraint("status IN ('pending','processed','failed')", name="ck_outbox_status"),
    )
    # The dispatcher poll index: oldest pending first.
    op.create_index(
        "ix_outbox_pending", "outbox_events", ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("notifications")
    op.drop_table("redirects")
    op.drop_table("seo_metadata")
    op.drop_table("cms_pages")
