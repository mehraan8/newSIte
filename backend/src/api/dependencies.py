"""Composition-root wiring for authorization (ADR-0011 §3).

Principal assembly is wired HERE, not inside a bounded context, because it needs
both Identity (platform role grants) and Merchant (seller memberships). Putting
it in either context would force that context to import the other and break the
Identity ↔ Merchant independence the architecture depends on. The composition
root is the one place allowed to know both.

    request
      → get_access_claims        (Identity: verify the token)
      → get_principal_factory    (here: wire the two ports)
      → Principal                (platform_permissions + memberships)
      → router / use case        (authorization decided from the snapshot)

All authorization I/O happens in this layer, once per request. Nothing
downstream — no policy, no domain object, no `Principal` method — performs any.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.security import PrincipalFactory
from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.identity.infrastructure.role_repository import SqlRoleGrantsQuery
from src.modules.identity.interface.dependencies import get_access_claims
from src.modules.merchant.infrastructure.membership_cache import CachedMembershipQuery
from src.modules.merchant.infrastructure.membership_query import SqlMembershipQuery
from src.shared_kernel.application.security import Principal
from src.shared_kernel.infrastructure.database import get_session
from src.shared_kernel.infrastructure.redis_client import get_redis


def get_principal_factory(
    session: AsyncSession = Depends(get_session),
) -> PrincipalFactory:
    """The factory, with both resolution ports attached.

    Memberships go through the cache-first adapter, so the common path costs a
    Redis read rather than a query, and a Redis outage degrades to a bounded
    database fallback instead of an outage (ADR-0011 §10.7).
    """
    return PrincipalFactory(
        role_grants=SqlRoleGrantsQuery(session),
        memberships=CachedMembershipQuery(SqlMembershipQuery(session), get_redis()),
    )


async def get_principal(
    claims: AccessTokenClaims = Depends(get_access_claims),
    factory: PrincipalFactory = Depends(get_principal_factory),
) -> Principal:
    """The authenticated caller's full capability set.

    Depends on `get_access_claims`, so an anonymous or malformed credential fails
    as 401 during authentication and never reaches an authorization check — a
    missing credential must not be reported as a permission problem.
    """
    return await factory.from_claims(claims)
