"""Read-only ACL to Selling. Only place Inventory references an offer; narrow SQL
existence check (no Python coupling). Becomes an HTTP call on extraction."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared_kernel.infrastructure.database import SessionFactory


class SqlSellingGateway:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] = SessionFactory) -> None:
        self._sf = session_factory

    async def offer_exists(self, offer_id: uuid.UUID) -> bool:
        async with self._sf() as session:
            found = await session.scalar(
                text("SELECT 1 FROM offers WHERE id = :id LIMIT 1"),
                {"id": str(offer_id)},
            )
            return found is not None
