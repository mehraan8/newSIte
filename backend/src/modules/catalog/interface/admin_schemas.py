"""HTTP request/response models for Catalog write (admin) endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.modules.catalog.application.write_dtos import ProductView


class VariantRequest(BaseModel):
    sku: str
    variant_axis_values: dict = Field(default_factory=dict)
    barcode: str | None = None


class CreateProductRequest(BaseModel):
    product_type_id: int
    category_id: int
    slug: str
    name_fa: str
    brand_id: int | None = None
    description: str | None = None
    is_featured: bool = False
    variants: list[VariantRequest] = Field(default_factory=list)


class UpdateProductRequest(BaseModel):
    name_fa: str | None = None
    description: str | None = None
    brand_id: int | None = None
    category_id: int | None = None
    is_featured: bool | None = None
    status: str | None = None


class VariantResponse(BaseModel):
    id: str
    sku: str
    variant_axis_values: dict
    barcode: str | None
    is_active: bool


class ProductResponse(BaseModel):
    id: str
    slug: str
    name_fa: str
    status: str
    product_type_id: int
    category_id: int
    brand_id: int | None
    description: str | None
    is_featured: bool
    variants: list[VariantResponse]

    @classmethod
    def from_view(cls, view: ProductView) -> "ProductResponse":
        return cls(
            id=view.id,
            slug=view.slug,
            name_fa=view.name_fa,
            status=view.status,
            product_type_id=view.product_type_id,
            category_id=view.category_id,
            brand_id=view.brand_id,
            description=view.description,
            is_featured=view.is_featured,
            variants=[VariantResponse(**v.__dict__) for v in view.variants],
        )
