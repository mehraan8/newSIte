"""Selling write-side ports. Application depends only on these abstractions."""

from __future__ import annotations

import uuid
from typing import Protocol

from src.modules.selling.domain.entities import Offer


class OfferRepository(Protocol):
    async def get_by_id(self, offer_id: uuid.UUID) -> Offer | None: ...
    async def add(self, offer: Offer) -> None: ...
    async def save(self, offer: Offer) -> None: ...
    async def find_active_for(
        self, seller_id: uuid.UUID, variant_id: uuid.UUID
    ) -> Offer | None: ...


class CatalogGateway(Protocol):
    """Anti-corruption boundary to the Catalog context. Read-only; the only way
    Selling learns about variants. Becomes an HTTP/gRPC call on extraction."""

    async def variant_exists(self, variant_id: uuid.UUID) -> bool: ...
