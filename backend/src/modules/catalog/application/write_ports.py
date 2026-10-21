"""Catalog write-side ports. The application depends only on these abstractions —
never on SQLAlchemy or any infrastructure type. Repositories return ONLY domain
objects (Product), never ORM rows.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from src.modules.catalog.domain.entities import Product


class ProductRepository(Protocol):
    async def get_by_id(self, product_id: uuid.UUID) -> Product | None: ...
    async def get_by_slug(self, slug: str) -> Product | None: ...
    async def add(self, product: Product) -> None: ...
    async def save(self, product: Product) -> None: ...
