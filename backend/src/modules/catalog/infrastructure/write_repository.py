"""SQLAlchemy adapter for the Catalog write side.

Rules honored:
  * returns ONLY domain objects (Product) — never ORM rows
  * EXPLICIT eager loading (selectinload) — no lazy IO in async
  * registers aggregates with the UoW tracker so events drain to the outbox
  * never commits (the UoW owns the transaction)
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.modules.catalog.domain.entities import Product, Variant
from src.modules.catalog.infrastructure.models import ProductModel, ProductVariantModel
from src.shared_kernel.application.unit_of_work import Tracker


def _variant_to_domain(row: ProductVariantModel) -> Variant:
    return Variant(
        row.sku,
        id=row.id,
        variant_axis_values=row.variant_axis_values or {},
        barcode=row.barcode,
        is_active=row.is_active,
    )


def _to_domain(row: ProductModel) -> Product:
    return Product(
        id=row.id,
        product_type_id=row.product_type_id,
        category_id=row.category_id,
        slug=row.slug,
        name_fa=row.name_fa,
        brand_id=row.brand_id,
        description=row.description,
        status=row.status,
        is_featured=row.is_featured,
        variants=[_variant_to_domain(v) for v in row.variants],
    )


class SqlProductRepository:
    def __init__(self, session: AsyncSession, tracker: Tracker | None = None) -> None:
        self._s = session
        self._tracker = tracker

    def _track(self, product: Product) -> None:
        if self._tracker is not None:
            self._tracker.track(product)

    async def _load_model(self, *, product_id=None, slug=None) -> ProductModel | None:
        stmt = select(ProductModel).options(selectinload(ProductModel.variants))
        if product_id is not None:
            stmt = stmt.where(ProductModel.id == product_id)
        else:
            stmt = stmt.where(ProductModel.slug == slug)
        stmt = stmt.where(ProductModel.deleted_at.is_(None))
        return await self._s.scalar(stmt)

    async def get_by_id(self, product_id: uuid.UUID) -> Product | None:
        row = await self._load_model(product_id=product_id)
        if row is None:
            return None
        product = _to_domain(row)
        self._track(product)
        return product

    async def get_by_slug(self, slug: str) -> Product | None:
        row = await self._load_model(slug=slug)
        if row is None:
            return None
        product = _to_domain(row)
        self._track(product)
        return product

    async def add(self, product: Product) -> None:
        model = ProductModel(
            id=product.id,
            product_type_id=product.product_type_id,
            brand_id=product.brand_id,
            category_id=product.category_id,
            slug=product.slug,
            name_fa=product.name_fa,
            description=product.description,
            status=product.status,
            is_featured=product.is_featured,
        )
        model.variants = [
            ProductVariantModel(
                id=v.id,
                sku=v.sku,
                barcode=v.barcode,
                variant_axis_values=v.variant_axis_values,
                is_active=v.is_active,
            )
            for v in product.variants
        ]
        self._s.add(model)
        await self._s.flush()
        self._track(product)

    async def save(self, product: Product) -> None:
        row = await self._load_model(product_id=product.id)
        if row is None:
            raise LookupError(f"product {product.id} not found for update")

        # Scalar columns.
        row.name_fa = product.name_fa
        row.description = product.description
        row.brand_id = product.brand_id
        row.category_id = product.category_id
        row.status = product.status
        row.is_featured = product.is_featured

        # Variant reconciliation by SKU (variants are eagerly loaded — no lazy IO).
        existing = {v.sku: v for v in row.variants}
        for dv in product.variants:
            ev = existing.get(dv.sku)
            if ev is None:
                row.variants.append(
                    ProductVariantModel(
                        id=dv.id,
                        sku=dv.sku,
                        barcode=dv.barcode,
                        variant_axis_values=dv.variant_axis_values,
                        is_active=dv.is_active,
                    )
                )
            else:
                ev.barcode = dv.barcode
                ev.variant_axis_values = dv.variant_axis_values
                ev.is_active = dv.is_active

        await self._s.flush()
        self._track(product)
