"""Read-side queries for the Catalog context."""

from __future__ import annotations

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.catalog.infrastructure.models import (
    BrandModel,
    CategoryModel,
    PriceModel,
    ProductModel,
    ProductTypeModel,
    ProductVariantModel,
)
from src.shared_kernel.domain.base import utcnow


def _active_price_window():
    now = utcnow()
    return and_(
        PriceModel.valid_from <= now,
        or_(PriceModel.valid_to.is_(None), PriceModel.valid_to > now),
    )


def _escape_like(value: str) -> str:
    """Neutralise LIKE metacharacters in user search input.

    `q` is bound as a parameter, so this was never SQL injection — but `%` and
    `_` are wildcards to the DATABASE, not to the parameter binder. A search for
    `%` matched every row and scanned the whole table, and a pattern of
    alternating wildcards is pathological for the matcher, on a PUBLIC
    unauthenticated endpoint. That is an availability problem, not an injection
    one, and it needs a different fix.

    Escaping also makes the endpoint behave the way a search box should: a
    shopper typing "50%" is looking for the characters "50%", not asking for a
    wildcard. The backslash must be escaped first, or it would escape the
    escapes added after it.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CatalogReadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    def _base_filter(
        self, type_code: str | None, category_slug: str | None, brand_slug: str | None, q: str | None
    ) -> Select:
        stmt = (
            select(ProductModel)
            .join(ProductTypeModel, ProductModel.product_type_id == ProductTypeModel.id)
            .outerjoin(CategoryModel, ProductModel.category_id == CategoryModel.id)
            .outerjoin(BrandModel, ProductModel.brand_id == BrandModel.id)
            .where(ProductModel.status == "active", ProductModel.deleted_at.is_(None))
        )
        if type_code:
            stmt = stmt.where(ProductTypeModel.code == type_code)
        if category_slug:
            stmt = stmt.where(CategoryModel.slug == category_slug)
        if brand_slug:
            stmt = stmt.where(BrandModel.slug == brand_slug)
        if q:
            stmt = stmt.where(
                ProductModel.name_fa.ilike(f"%{_escape_like(q)}%", escape="\\")
            )
        return stmt

    async def list_products(
        self,
        *,
        type_code: str | None = None,
        category_slug: str | None = None,
        brand_slug: str | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ProductModel], int, dict]:
        base = self._base_filter(type_code, category_slug, brand_slug, q)

        total = await self._s.scalar(
            select(func.count()).select_from(base.order_by(None).subquery())
        )
        rows = list(
            (await self._s.scalars(base.order_by(ProductModel.created_at.desc()).limit(limit).offset(offset))).all()
        )

        # Cheapest active price per product (single grouped query).
        price_map: dict = {}
        if rows:
            ids = [r.id for r in rows]
            price_q = (
                select(ProductVariantModel.product_id, func.min(PriceModel.amount))
                .join(PriceModel, PriceModel.variant_id == ProductVariantModel.id)
                .where(ProductVariantModel.product_id.in_(ids), _active_price_window())
                .group_by(ProductVariantModel.product_id)
            )
            for pid, amount in (await self._s.execute(price_q)).all():
                price_map[pid] = int(amount)

        return rows, int(total or 0), price_map

    async def get_by_slug(self, slug: str) -> ProductModel | None:
        return await self._s.scalar(
            select(ProductModel).where(
                ProductModel.slug == slug,
                ProductModel.status == "active",
                ProductModel.deleted_at.is_(None),
            )
        )

    @staticmethod
    def current_price(variant: ProductVariantModel) -> PriceModel | None:
        now = utcnow()
        valid = [
            p for p in variant.prices
            if p.valid_from <= now and (p.valid_to is None or p.valid_to > now)
        ]
        return max(valid, key=lambda p: p.valid_from) if valid else None
