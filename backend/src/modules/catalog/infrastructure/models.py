"""SQLAlchemy models for the Catalog context (mirrors migrations 0002/0003).

Only the tables touched by the implemented read endpoints are mapped here.
Nothing in this file is vertical-specific — cosmetics vs. mobile is pure data.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared_kernel.infrastructure.database import Base


class ProductTypeModel(Base):
    __tablename__ = "product_types"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AttributeModel(Base):
    __tablename__ = "attributes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(120), nullable=False)
    data_type: Mapped[str] = mapped_column(String(20), nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(20))
    is_filterable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_variant_axis: Mapped[bool] = mapped_column(Boolean, default=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)


class AttributeOptionModel(Base):
    __tablename__ = "attribute_options"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attribute_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("attributes.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(String(120), nullable=False)
    label_fa: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class BrandModel(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(120), nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(String(120))
    logo_url: Mapped[Optional[str]] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class CategoryModel(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("categories.id", ondelete="RESTRICT")
    )
    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(140), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductModel(Base):
    __tablename__ = "products"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product_types.id"), nullable=False
    )
    brand_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("brands.id"))
    category_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("categories.id"), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product_type: Mapped[ProductTypeModel] = relationship(lazy="joined")
    brand: Mapped[Optional[BrandModel]] = relationship(lazy="joined")
    category: Mapped[CategoryModel] = relationship(lazy="joined")
    attribute_values: Mapped[list["ProductAttributeValueModel"]] = relationship(
        back_populates="product", lazy="selectin"
    )
    variants: Mapped[list["ProductVariantModel"]] = relationship(
        back_populates="product", lazy="selectin"
    )


class ProductAttributeValueModel(Base):
    __tablename__ = "product_attribute_values"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    attribute_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("attributes.id"), nullable=False)
    value_string: Mapped[Optional[str]] = mapped_column(String(500))
    value_int: Mapped[Optional[int]] = mapped_column(BigInteger)
    value_decimal: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4))
    value_bool: Mapped[Optional[bool]] = mapped_column(Boolean)
    value_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    option_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("attribute_options.id"))

    product: Mapped[ProductModel] = relationship(back_populates="attribute_values")
    attribute: Mapped[AttributeModel] = relationship(lazy="joined")
    option: Mapped[Optional[AttributeOptionModel]] = relationship(lazy="joined")


class ProductVariantModel(Base):
    __tablename__ = "product_variants"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    barcode: Mapped[Optional[str]] = mapped_column(String(64))
    variant_axis_values: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    product: Mapped[ProductModel] = relationship(back_populates="variants")
    prices: Mapped[list["PriceModel"]] = relationship(lazy="selectin")


class PriceModel(Base):
    __tablename__ = "prices"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="IRR")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0), nullable=False)
    compare_at_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 0))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
