"""Command/result DTOs for Catalog write use cases (framework-agnostic).

Application boundary types — neither domain entities nor ORM rows cross out of
the use cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.modules.catalog.domain.entities import Product


@dataclass(frozen=True)
class VariantInput:
    sku: str
    variant_axis_values: dict = field(default_factory=dict)
    barcode: str | None = None


@dataclass(frozen=True)
class CreateProductCommand:
    product_type_id: int
    category_id: int
    slug: str
    name_fa: str
    brand_id: int | None = None
    description: str | None = None
    is_featured: bool = False
    variants: tuple[VariantInput, ...] = ()


@dataclass(frozen=True)
class UpdateProductCommand:
    name_fa: str | None = None
    description: str | None = None
    brand_id: int | None = None
    category_id: int | None = None
    is_featured: bool | None = None
    status: str | None = None


@dataclass(frozen=True)
class VariantView:
    id: str
    sku: str
    variant_axis_values: dict
    barcode: str | None
    is_active: bool


@dataclass(frozen=True)
class ProductView:
    id: str
    slug: str
    name_fa: str
    status: str
    product_type_id: int
    category_id: int
    brand_id: int | None
    description: str | None
    is_featured: bool
    variants: tuple[VariantView, ...]

    @classmethod
    def from_domain(cls, product: Product) -> "ProductView":
        return cls(
            id=str(product.id),
            slug=product.slug,
            name_fa=product.name_fa,
            status=product.status,
            product_type_id=product.product_type_id,
            category_id=product.category_id,
            brand_id=product.brand_id,
            description=product.description,
            is_featured=product.is_featured,
            variants=tuple(
                VariantView(
                    id=str(v.id),
                    sku=v.sku,
                    variant_axis_values=v.variant_axis_values,
                    barcode=v.barcode,
                    is_active=v.is_active,
                )
                for v in product.variants
            ),
        )
