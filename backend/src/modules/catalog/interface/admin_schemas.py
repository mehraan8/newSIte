"""HTTP request/response models for Catalog write (admin) endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.modules.catalog.application.write_dtos import ProductView


# Lengths mirror the database columns (products.slug varchar(200),
# products.name_fa varchar(255), product_variants.sku/barcode), so an oversized
# value is a 422 from the schema instead of a StringDataRightTruncationError
# raised at flush time and surfaced as a 500. Positive-int constraints likewise
# reject ids that could never satisfy the foreign keys.
_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


# extra="forbid" throughout, matching CheckoutBody and CreateOfferRequest. A
# client sending `id`, `status` or `owner_id` on create was previously accepted
# and the field silently dropped — safe, but it hides an attempted privilege
# injection instead of surfacing it. Rejecting makes the attempt visible in logs
# and to the caller, and stops a future field rename from turning a
# silently-ignored input into a silently-honoured one.
class VariantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64)
    variant_axis_values: dict = Field(default_factory=dict)
    barcode: str | None = Field(default=None, max_length=64)


class CreateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_type_id: int = Field(ge=1)
    category_id: int = Field(ge=1)
    # Pattern mirrors the Slug value object, which raises InvalidSlugError.
    slug: str = Field(min_length=1, max_length=200, pattern=_SLUG_PATTERN)
    name_fa: str = Field(min_length=1, max_length=255)
    brand_id: int | None = Field(default=None, ge=1)
    description: str | None = None
    is_featured: bool = False
    variants: list[VariantRequest] = Field(default_factory=list, max_length=200)


class UpdateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_fa: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    brand_id: int | None = Field(default=None, ge=1)
    category_id: int | None = Field(default=None, ge=1)
    is_featured: bool | None = None
    # Mirrors ProductStatus.ALL, which the aggregate enforces with a ValueError.
    status: Literal["draft", "active", "archived"] | None = None


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
