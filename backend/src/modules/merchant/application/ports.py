"""Ports the Merchant application layer depends on.

Merchant-internal only. The Shared Kernel `MembershipQuery` port — the one
Principal assembly consumes — is deliberately NOT implemented here: wiring it to
this database is Phase 1F, and until then `PrincipalFactory` keeps
`memberships={}`, which is fail-closed.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from src.modules.merchant.domain.entities import Seller, SellerMember


class SellerRepository(Protocol):
    async def get(self, seller_id: uuid.UUID) -> Seller | None: ...
    async def get_by_code(self, code: str) -> Seller | None: ...
    async def add(self, seller: Seller) -> None: ...
    async def save(self, seller: Seller) -> None:
        """Version-guarded write. Raises ConcurrencyConflictError on a lost race."""
        ...


class SellerMemberRepository(Protocol):
    async def get(
        self, seller_id: uuid.UUID, user_id: uuid.UUID
    ) -> SellerMember | None: ...
    async def list_for_user(self, user_id: uuid.UUID) -> list[SellerMember]:
        """Every membership a user holds — the shape Phase 1F will resolve from."""
        ...
    async def list_for_seller(self, seller_id: uuid.UUID) -> list[SellerMember]: ...
    async def add(self, member: SellerMember) -> None:
        """Raises ConflictError if the (seller_id, user_id) pair already exists."""
        ...
    async def save(self, member: SellerMember) -> None: ...
