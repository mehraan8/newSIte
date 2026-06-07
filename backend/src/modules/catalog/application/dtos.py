"""Catalog read DTOs. The same shapes describe a lipstick or a smartphone —
the `attributes` list is driven by the product's type, never hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AttributeValueDTO:
    code: str
    name_fa: str
    unit: str | None
    value: object  # str | int | float | bool | option-label


@dataclass(frozen=True)
class VariantDTO:
    id: str
    sku: str
    axis: dict
    price: int | None
    compare_at: int | None
    currency: str


@dataclass(frozen=True)
class ProductSummaryDTO:
    id: str
    slug: str
    name_fa: str
    type_code: str
    brand: str | None
    category: str | None
    from_price: int | None
    currency: str


@dataclass(frozen=True)
class ProductDetailDTO:
    id: str
    slug: str
    name_fa: str
    description: str | None
    type_code: str
    brand: str | None
    category: str | None
    attributes: list[AttributeValueDTO] = field(default_factory=list)
    variants: list[VariantDTO] = field(default_factory=list)


@dataclass(frozen=True)
class Page:
    items: list
    total: int
    limit: int
    offset: int
