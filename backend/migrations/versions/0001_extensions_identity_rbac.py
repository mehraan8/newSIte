"""extensions, identity & rbac

Creates PostgreSQL extensions plus the Identity context (users, otp_challenges,
refresh_tokens, addresses) and the Admin/RBAC context (roles, permissions,
role_permissions, user_roles, audit_logs).

Revision ID: 0001
Revises:
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    # --- Extensions -------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")      # gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")    # EXCLUDE constraints
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")       # fuzzy text search

    # --- Identity: users --------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("mobile", sa.String(15), nullable=False),
        sa.Column("full_name", sa.String(150)),
        sa.Column("email", sa.String(255)),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("is_phone_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint("status IN ('active','blocked','pending')", name="ck_users_status"),
        sa.CheckConstraint(r"mobile ~ '^\+989\d{9}$'", name="ck_users_mobile_format"),
    )
    op.create_index("ix_users_mobile", "users", ["mobile"], unique=True)
    op.create_index("ix_users_status", "users", ["status"])
    op.create_index(
        "uq_users_email", "users", ["email"], unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    # --- Identity: otp_challenges (audit / anti-abuse) --------------------
    op.create_table(
        "otp_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("mobile", sa.String(15), nullable=False),
        sa.Column("code_hash", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.SmallInteger, nullable=False, server_default="5"),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("consumed_at", TS),
        sa.Column("ip_address", postgresql.INET),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint("purpose IN ('login','reset','verify')", name="ck_otp_purpose"),
    )
    op.create_index("ix_otp_mobile_created", "otp_challenges", ["mobile", sa.text("created_at DESC")])
    op.create_index("ix_otp_expires", "otp_challenges", ["expires_at"])

    # --- Identity: refresh_tokens ----------------------------------------
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_info", sa.String(255)),
        sa.Column("ip_address", postgresql.INET),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_rt_user", "refresh_tokens", ["user_id"])
    op.create_index("ix_rt_token_hash", "refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_rt_family", "refresh_tokens", ["family_id"])

    # --- Identity: addresses ---------------------------------------------
    op.create_table(
        "addresses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receiver_name", sa.String(150), nullable=False),
        sa.Column("receiver_mobile", sa.String(15), nullable=False),
        sa.Column("province", sa.String(50), nullable=False),
        sa.Column("city", sa.String(80), nullable=False),
        sa.Column("postal_code", sa.String(10), nullable=False),
        sa.Column("line", sa.String(500), nullable=False),
        sa.Column("plaque", sa.String(20)),
        sa.Column("unit", sa.String(20)),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(r"postal_code ~ '^\d{10}$'", name="ck_addr_postal"),
    )
    op.create_index("ix_addr_user", "addresses", ["user_id"])
    op.create_index(
        "uq_addr_one_default", "addresses", ["user_id"], unique=True,
        postgresql_where=sa.text("is_default"),
    )

    # --- RBAC: roles / permissions ---------------------------------------
    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name_fa", sa.String(100), nullable=False),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("code", name="uq_roles_code"),
    )
    op.create_table(
        "permissions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("description", sa.String(255)),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.BigInteger, nullable=False),
        sa.Column("permission_id", sa.BigInteger, nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.BigInteger, nullable=False),
        sa.Column("assigned_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )

    # --- RBAC: audit_logs (append-only; partition by month later) --------
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(50)),
        sa.Column("entity_id", sa.String(64)),
        sa.Column("before", postgresql.JSONB),
        sa.Column("after", postgresql.JSONB),
        sa.Column("ip_address", postgresql.INET),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_audit_actor", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_entity", "audit_logs", ["entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("user_roles")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("addresses")
    op.drop_table("refresh_tokens")
    op.drop_table("otp_challenges")
    op.drop_table("users")
    # Extensions are left in place intentionally (may be shared by other DBs).
