"""Catalog write-side domain model. Pure Python — no ORM, no framework.

Product is the aggregate root; Variant is an entity inside it. All mutation goes
through aggregate methods, which record domain events drained to the outbox by
the UoW.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from src.shared_kernel.domain.base import AggregateRoot, DomainEvent, new_uuid
from src.shared_kernel.domain.value_objects import Slug


class ProductStatus:
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"

    ALL = frozenset({DRAFT, ACTIVE, ARCHIVED})


# --- Domain events (PII-free: product data only) --------------------------
@dataclass(frozen=True)
class ProductCreated(DomainEvent):
    product_id: uuid.UUID | None = None
    slug: str = ""
    name_fa: str = ""
    product_type_id: int | None = None
    status: str = ProductStatus.DRAFT


@dataclass(frozen=True)
class ProductUpdated(DomainEvent):
    product_id: uuid.UUID | None = None
    slug: str = ""
    status: str = ""
    changed_fields: tuple[str, ...] = ()


# --- Variant entity (lives inside the Product aggregate) -------------------
class Variant:
    def __init__(
        self,
        sku: str,
        *,
        id: uuid.UUID | None = None,
        variant_axis_values: dict | None = None,
        barcode: str | None = None,
        is_active: bool = True,
    ) -> None:
        if not sku or not sku.strip():
            raise ValueError("variant sku is required")
        self.id = id or new_uuid()
        self.sku = sku.strip()
        self.variant_axis_values = variant_axis_values or {}
        self.barcode = barcode
        self.is_active = is_active


# --- Product aggregate root -----------------------------------------------
class Product(AggregateRoot):
    def __init__(
        self,
        *,
        id: uuid.UUID,
        product_type_id: int,
        category_id: int,
        slug: str,
        name_fa: str,
        brand_id: int | None = None,
        description: str | None = None,
        status: str = ProductStatus.DRAFT,
        is_featured: bool = False,
        variants: list[Variant] | None = None,
    ) -> None:
        super().__init__()
        if status not in ProductStatus.ALL:
            raise ValueError(f"invalid product status {status!r}")
        self.id = id
        self.product_type_id = product_type_id
        self.category_id = category_id
        self.slug = str(Slug(slug))
        self.name_fa = _require(name_fa, "name_fa")
        self.brand_id = brand_id
        self.description = description
        self.status = status
        self.is_featured = is_featured
        self.variants: list[Variant] = variants or []

    # -- factory --
    @classmethod
    def create(
        cls,
        *,
        product_type_id: int,
        category_id: int,
        slug: str,
        name_fa: str,
        brand_id: int | None = None,
        description: str | None = None,
        is_featured: bool = False,
    ) -> "Product":
        product = cls(
            id=new_uuid(),
            product_type_id=product_type_id,
            category_id=category_id,
            slug=slug,
            name_fa=name_fa,
            brand_id=brand_id,
            description=description,
            status=ProductStatus.DRAFT,
            is_featured=is_featured,
        )
        product.record_event(
            ProductCreated(
                product_id=product.id,
                slug=product.slug,
                name_fa=product.name_fa,
                product_type_id=product.product_type_id,
                status=product.status,
            )
        )
        return product

    # -- behavior --
    def add_variant(
        self, sku: str, *, variant_axis_values: dict | None = None, barcode: str | None = None
    ) -> Variant:
        if any(v.sku == sku.strip() for v in self.variants):
            raise ValueError(f"duplicate sku '{sku}' in product")
        variant = Variant(sku, variant_axis_values=variant_axis_values, barcode=barcode)
        self.variants.append(variant)
        return variant

    def update_details(
        self,
        *,
        name_fa: str | None = None,
        description: str | None = None,
        brand_id: int | None = None,
        category_id: int | None = None,
        is_featured: bool | None = None,
    ) -> None:
        changed: list[str] = []
        if name_fa is not None and name_fa != self.name_fa:
            self.name_fa = _require(name_fa, "name_fa"); changed.append("name_fa")
        if description is not None and description != self.description:
            self.description = description; changed.append("description")
        if brand_id is not None and brand_id != self.brand_id:
            self.brand_id = brand_id; changed.append("brand_id")
        if category_id is not None and category_id != self.category_id:
            self.category_id = category_id; changed.append("category_id")
        if is_featured is not None and is_featured != self.is_featured:
            self.is_featured = is_featured; changed.append("is_featured")
        if changed:
            self._emit_updated(tuple(changed))

    def change_status(self, status: str) -> None:
        if status not in ProductStatus.ALL:
            raise ValueError(f"invalid product status {status!r}")
        if status != self.status:
            self.status = status
            self._emit_updated(("status",))

    def _emit_updated(self, changed_fields: tuple[str, ...]) -> None:
        self.record_event(
            ProductUpdated(
                product_id=self.id,
                slug=self.slug,
                status=self.status,
                changed_fields=changed_fields,
            )
        )


def _require(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()
