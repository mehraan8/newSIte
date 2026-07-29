"""Anti-corruption adapter to the Catalog context.

This is the ONLY place Selling touches Catalog data, and it does so read-only.
It deliberately uses a narrow SQL existence check (not Catalog's ORM/domain) so
Selling has zero Python coupling to Catalog. On extraction this method body is
swapped for an HTTP/gRPC call to the Catalog service — nothing else changes.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared_kernel.infrastructure.database import SessionFactory


class SqlCatalogVariantGateway:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] = SessionFactory) -> None:
        self._sf = session_factory

    async def variant_exists(self, variant_id: uuid.UUID) -> bool:
        async with self._sf() as session:
            found = await session.scalar(
                text(
                    "SELECT 1 FROM product_variants "
                    "WHERE id = :id AND is_active = true LIMIT 1"
                ),
                {"id": str(variant_id)},
            )
            return found is not None
