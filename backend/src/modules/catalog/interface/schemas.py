"""Catalog HTTP response models."""

from __future__ import annotations

from typing import Optional, Any

from pydantic import BaseModel


class AttributeValueOut(BaseModel):
    code: str
    name_fa: str
    unit: Optional[str] = None
    value: Any


class VariantOut(BaseModel):
    id: str
    sku: str
    axis: dict
    price: Optional[int] = None
    compare_at: Optional[int] = None
    currency: str = "IRR"


class ProductSummaryOut(BaseModel):
    id: str
    slug: str
    name_fa: str
    type_code: str
    brand: Optional[str] = None
    category: Optional[str] = None
    from_price: Optional[int] = None
    currency: str = "IRR"


class ProductDetailOut(BaseModel):
    id: str
    slug: str
    name_fa: str
    description: Optional[str] = None
    type_code: str
    brand: Optional[str] = None
    category: Optional[str] = None
    attributes: list[AttributeValueOut] = []
    variants: list[VariantOut] = []


class ProductListOut(BaseModel):
    items: list[ProductSummaryOut]
    total: int
    limit: int
    offset: int
