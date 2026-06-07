"""Catalog read use cases. Map ORM rows to vertical-agnostic DTOs, resolving
each product's attribute schema from its type at runtime.
"""

from __future__ import annotations

from src.modules.catalog.application.dtos import (
    AttributeValueDTO,
    Page,
    ProductDetailDTO,
    ProductSummaryDTO,
    VariantDTO,
)
from src.modules.catalog.infrastructure.models import (
    ProductAttributeValueModel,
    ProductModel,
    ProductVariantModel,
)
from src.modules.catalog.infrastructure.repository import CatalogReadRepository
from src.shared_kernel.application.errors import NotFoundError


def _resolve_value(pav: ProductAttributeValueModel) -> object:
    if pav.option is not None:
        return pav.option.label_fa
    for field in ("value_string", "value_int", "value_bool", "value_date"):
        v = getattr(pav, field)
        if v is not None:
            return v
    if pav.value_decimal is not None:
        return float(pav.value_decimal)
    return None


def _variant_dto(repo: CatalogReadRepository, v: ProductVariantModel) -> VariantDTO:
    price = repo.current_price(v)
    return VariantDTO(
        id=str(v.id),
        sku=v.sku,
        axis=v.variant_axis_values or {},
        price=int(price.amount) if price else None,
        compare_at=int(price.compare_at_amount) if price and price.compare_at_amount else None,
        currency=price.currency if price else "IRR",
    )


class ListProducts:
    def __init__(self, repo: CatalogReadRepository) -> None:
        self._repo = repo

    async def execute(self, **filters) -> Page:
        limit = min(int(filters.pop("limit", 20)), 100)
        offset = int(filters.pop("offset", 0))
        rows, total, price_map = await self._repo.list_products(limit=limit, offset=offset, **filters)
        items = [
            ProductSummaryDTO(
                id=str(p.id),
                slug=p.slug,
                name_fa=p.name_fa,
                type_code=p.product_type.code,
                brand=p.brand.name_fa if p.brand else None,
                category=p.category.name_fa if p.category else None,
                from_price=price_map.get(p.id),
                currency="IRR",
            )
            for p in rows
        ]
        return Page(items=items, total=total, limit=limit, offset=offset)


class GetProduct:
    def __init__(self, repo: CatalogReadRepository) -> None:
        self._repo = repo

    async def execute(self, slug: str) -> ProductDetailDTO:
        p: ProductModel | None = await self._repo.get_by_slug(slug)
        if p is None:
            raise NotFoundError("محصول یافت نشد.")
        attributes = [
            AttributeValueDTO(
                code=pav.attribute.code,
                name_fa=pav.attribute.name_fa,
                unit=pav.attribute.unit,
                value=_resolve_value(pav),
            )
            for pav in p.attribute_values
        ]
        variants = [_variant_dto(self._repo, v) for v in p.variants if v.is_active]
        return ProductDetailDTO(
            id=str(p.id),
            slug=p.slug,
            name_fa=p.name_fa,
            description=p.description,
            type_code=p.product_type.code,
            brand=p.brand.name_fa if p.brand else None,
            category=p.category.name_fa if p.category else None,
            attributes=attributes,
            variants=variants,
        )
