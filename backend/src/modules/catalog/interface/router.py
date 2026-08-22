"""Public Catalog endpoints (cacheable read surface)."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.catalog.application.use_cases import GetProduct, ListProducts
from src.modules.catalog.infrastructure.repository import CatalogReadRepository
from src.modules.catalog.interface.schemas import ProductDetailOut, ProductListOut
from src.shared_kernel.infrastructure.database import get_session

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _repo(session: AsyncSession = Depends(get_session)) -> CatalogReadRepository:
    return CatalogReadRepository(session)


@router.get("/products", response_model=ProductListOut)
async def list_products(
    # Length caps on a PUBLIC, unauthenticated endpoint. `q` was unbounded, so a
    # multi-megabyte query string became a multi-megabyte pattern match against
    # every active product — cheap to send, expensive to serve. The filter codes
    # compare for equality and so are cheap, but are capped to the column widths
    # they are matched against for the same reason.
    type: str | None = Query(default=None, max_length=64, description="product_type code"),
    category: str | None = Query(default=None, max_length=200),
    brand: str | None = Query(default=None, max_length=200),
    q: str | None = Query(default=None, max_length=100, description="search by name"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    repo: CatalogReadRepository = Depends(_repo),
) -> ProductListOut:
    page = await ListProducts(repo).execute(
        type_code=type, category_slug=category, brand_slug=brand, q=q, limit=limit, offset=offset
    )
    return ProductListOut(
        items=[asdict(i) for i in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/products/{slug}", response_model=ProductDetailOut)
async def get_product(slug: str, repo: CatalogReadRepository = Depends(_repo)) -> ProductDetailOut:
    detail = await GetProduct(repo).execute(slug)
    return ProductDetailOut(**asdict(detail))
