"""Merchant's implementation of the Shared Kernel `MembershipQuery` port.

Merchant owns membership resolution (ADR-0011 §4, §7). The Shared Kernel defines
the port; this satisfies it; the composition root wires the two. Identity is not
involved and is never imported — the authenticated subject arrives from the
token, so there is no reason to touch `users` here.

Narrow SQL rather than ORM graphs, matching the repository's gateway adapters.
Both queries are single statements: `memberships_for` is one join served by
`ix_seller_members_user`, and `seller_states` resolves a whole batch with
`= ANY(:ids)` rather than one query per seller.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared_kernel.application.security import (
    SellerAuthorizationState,
    SellerStatus,
)

# ONE join, ONE round trip. Both status filters are applied in SQL so an inactive
# membership or a suspended seller never reaches the application at all — the
# database is the first place this fails closed, not the last.
_ACTIVE_MEMBERSHIPS = text(
    """
    SELECT sm.seller_id, sm.member_role_code
      FROM seller_members sm
      JOIN sellers s ON s.id = sm.seller_id
     WHERE sm.user_id = :user_id
       AND sm.status  = 'active'
       AND s.status   = 'active'
     ORDER BY sm.seller_id
    """
)

# Batched: `= ANY` keeps this to one statement for N sellers.
_SELLER_STATES = text(
    """
    SELECT id, status, tenant_id, version
      FROM sellers
     WHERE id = ANY(:seller_ids)
    """
)


class SqlMembershipQuery:
    """Reads memberships and seller authorization state. Never writes."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def memberships_for(self, user_id: uuid.UUID) -> Mapping[uuid.UUID, str]:
        """seller_id -> member_role_code, for ACTIVE memberships in ACTIVE sellers.

        An empty mapping is a real answer — a user with no memberships — and the
        caching layer above must be able to store it (ADR-0011 §10.1).
        """
        rows = await self._s.execute(_ACTIVE_MEMBERSHIPS, {"user_id": str(user_id)})
        return {row.seller_id: row.member_role_code for row in rows}

    async def seller_states(
        self, seller_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, SellerAuthorizationState]:
        """Authorization state for the given sellers, in one query.

        A seller whose stored status is not a value this build understands is
        OMITTED rather than guessed at: the caller treats a missing entry as
        conferring nothing, so an unknown status fails closed.
        """
        if not seller_ids:
            return {}
        rows = await self._s.execute(
            _SELLER_STATES, {"seller_ids": [str(sid) for sid in seller_ids]}
        )
        states: dict[uuid.UUID, SellerAuthorizationState] = {}
        for row in rows:
            try:
                status = SellerStatus(row.status)
            except ValueError:
                continue  # unknown status -> no state -> no authority
            states[row.id] = SellerAuthorizationState(
                seller_id=row.id,
                status=status,
                tenant_id=row.tenant_id,
                version=row.version,
            )
        return states
