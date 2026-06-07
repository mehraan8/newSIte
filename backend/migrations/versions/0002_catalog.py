"""catalog (vertical-agnostic core)

product_types, attributes, attribute_options, product_type_attributes, brands,
categories (tree), products, product_attribute_values (typed EAV),
product_variants, product_media.

Switching cosmetics -> mobile is a data operation against product_types /
attributes / product_type_attributes -- no schema in this file changes.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    # --- product_types (the migration switch) ----------------------------
    op.create_table(
        "product_types",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name_fa", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("code", name="uq_ptype_code"),
    )

    # --- attributes (reusable definitions) -------------------------------
    op.create_table(
        "attributes",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name_fa", sa.String(120), nullable=False),
        sa.Column("data_type", sa.String(20), nullable=False),
        sa.Column("unit", sa.String(20)),
        sa.Column("is_filterable", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_variant_axis", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_required", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("code", name="uq_attr_code"),
        sa.CheckConstraint(
            "data_type IN ('string','int','decimal','bool','enum','date')",
            name="ck_attr_datatype",
        ),
    )
    op.create_index(
        "ix_attr_filterable", "attributes", ["id"],
        postgresql_where=sa.text("is_filterable"),
    )

    # --- attribute_options (for enum attributes) -------------------------
    op.create_table(
        "attribute_options",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("attribute_id", sa.BigInteger, nullable=False),
        sa.Column("value", sa.String(120), nullable=False),
        sa.Column("label_fa", sa.String(120), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["attribute_id"], ["attributes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("attribute_id", "value", name="uq_attropt_attr_value"),
    )
    op.create_index("ix_attropt_attr", "attribute_options", ["attribute_id"])

    # --- product_type_attributes (which attrs apply to a type) -----------
    op.create_table(
        "product_type_attributes",
        sa.Column("product_type_id", sa.BigInteger, nullable=False),
        sa.Column("attribute_id", sa.BigInteger, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_required", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["product_type_id"], ["product_types.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attribute_id"], ["attributes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("product_type_id", "attribute_id"),
    )

    # --- brands -----------------------------------------------------------
    op.create_table(
        "brands",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(140), nullable=False),
        sa.Column("name_fa", sa.String(120), nullable=False),
        sa.Column("name_en", sa.String(120)),
        sa.Column("logo_url", sa.String(500)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("slug", name="uq_brand_slug"),
    )

    # --- categories (tree via materialized path) -------------------------
    op.create_table(
        "categories",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("parent_id", sa.BigInteger),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("name_fa", sa.String(140), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("depth", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["parent_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("slug", name="uq_cat_slug"),
    )
    op.create_index("ix_cat_parent", "categories", ["parent_id"])
    # Prefix/subtree lookups on the materialized path.
    op.create_index(
        "ix_cat_path", "categories", ["path"],
        postgresql_ops={"path": "varchar_pattern_ops"},
    )

    # --- products (aggregate root, universal fields only) ----------------
    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("product_type_id", sa.BigInteger, nullable=False),
        sa.Column("brand_id", sa.BigInteger),
        sa.Column("category_id", sa.BigInteger, nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("name_fa", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("is_featured", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["product_type_id"], ["product_types.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("slug", name="uq_prod_slug"),
        sa.CheckConstraint("status IN ('draft','active','archived')", name="ck_prod_status"),
    )
    op.create_index("ix_prod_type", "products", ["product_type_id"])
    op.create_index("ix_prod_category", "products", ["category_id"])
    op.create_index("ix_prod_brand", "products", ["brand_id"])
    op.create_index(
        "ix_prod_active", "products", ["status"],
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_prod_name_trgm", "products", ["name_fa"],
        postgresql_using="gin", postgresql_ops={"name_fa": "gin_trgm_ops"},
    )

    # --- product_attribute_values (typed EAV) ----------------------------
    op.create_table(
        "product_attribute_values",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attribute_id", sa.BigInteger, nullable=False),
        sa.Column("value_string", sa.String(500)),
        sa.Column("value_int", sa.BigInteger),
        sa.Column("value_decimal", sa.Numeric(18, 4)),
        sa.Column("value_bool", sa.Boolean),
        sa.Column("value_date", sa.Date),
        sa.Column("option_id", sa.BigInteger),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attribute_id"], ["attributes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["option_id"], ["attribute_options.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("product_id", "attribute_id", name="uq_pav_product_attr"),
        sa.CheckConstraint(
            "num_nonnulls(value_string, value_int, value_decimal, value_bool, value_date, option_id) = 1",
            name="ck_pav_one_value",
        ),
    )
    # Faceted-search support: index by attribute + value per type.
    op.create_index("ix_pav_attr_int", "product_attribute_values", ["attribute_id", "value_int"])
    op.create_index("ix_pav_attr_string", "product_attribute_values", ["attribute_id", "value_string"])
    op.create_index("ix_pav_attr_option", "product_attribute_values", ["attribute_id", "option_id"])

    # --- product_variants (SKUs) -----------------------------------------
    op.create_table(
        "product_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("barcode", sa.String(64)),
        sa.Column("variant_axis_values", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("sku", name="uq_var_sku"),
    )
    op.create_index("ix_var_product", "product_variants", ["product_id"])
    op.create_index(
        "uq_var_barcode", "product_variants", ["barcode"], unique=True,
        postgresql_where=sa.text("barcode IS NOT NULL"),
    )
    op.create_index(
        "ix_var_axis", "product_variants", ["variant_axis_values"], postgresql_using="gin",
    )

    # --- product_media ----------------------------------------------------
    op.create_table(
        "product_media",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True)),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("alt_fa", sa.String(255)),
        sa.Column("media_type", sa.String(10), nullable=False, server_default="image"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("media_type IN ('image','video')", name="ck_media_type"),
    )
    op.create_index("ix_media_product", "product_media", ["product_id"])
    op.create_index(
        "uq_media_primary", "product_media", ["product_id"], unique=True,
        postgresql_where=sa.text("is_primary"),
    )


def downgrade() -> None:
    op.drop_table("product_media")
    op.drop_table("product_variants")
    op.drop_table("product_attribute_values")
    op.drop_table("products")
    op.drop_table("categories")
    op.drop_table("brands")
    op.drop_table("product_type_attributes")
    op.drop_table("attribute_options")
    op.drop_table("attributes")
    op.drop_table("product_types")
