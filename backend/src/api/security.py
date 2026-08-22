"""Principal assembly — the composition root's authorization seam (ADR-0011 §3).

The four-layer model is: authentication -> PRINCIPAL ASSEMBLY -> router coarse
check -> application policy. This module is the second layer, and the only place
where authorization state is built.

Everything that costs I/O happens here, once per request, so policies downstream
stay pure and synchronous (ADR-0011 §9). Placing assembly behind ports is what
keeps the storage choice swappable: platform permissions arrive through
`RoleGrantsQuery` and seller authority through `MembershipQuery`, and neither the
Principal nor any policy can tell whether an answer came from PostgreSQL, Redis,
or a future remote service.

This module is the composition root, so it may legitimately know both Identity
and Merchant. The contexts themselves remain independent of each other.

Principal is EXPLICIT (ADR-0011 principle 4): this module returns one, and
callers pass it down. It is never stashed in a ContextVar, module global, or
request attribute.

SCOPE (Phase 1F): assembly resolves platform permissions AND seller memberships.
It is still NOT attached to any route — route classification and enforcement are
Phase 1I/1K, and the policies that consume `memberships` arrive with Phase 1G.
"""

from __future__ import annotations

import logging
import uuid

from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.merchant.application.member_permissions import permissions_for
from src.shared_kernel.application.permissions import Permission, from_code
from src.shared_kernel.application.security import (
    MembershipQuery,
    Principal,
    RoleGrantsQuery,
    SellerGrant,
)

logger = logging.getLogger("api.security")


class PrincipalFactory:
    """Builds a `Principal` from validated access-token claims.

    Takes ports, not adapters, so the same factory serves the in-process monolith
    and an extracted service.
    """

    def __init__(
        self,
        role_grants: RoleGrantsQuery,
        memberships: MembershipQuery | None = None,
    ) -> None:
        self._role_grants = role_grants
        # Optional so the factory can be constructed without Merchant wiring
        # (unit tests, and any caller that resolves no seller-scoped route).
        # Absent means "no memberships", which denies every seller-scoped
        # question rather than allowing it.
        self._memberships = memberships

    async def from_claims(self, claims: AccessTokenClaims) -> Principal:
        platform_permissions = await self._resolve_platform_permissions(
            claims.role_codes
        )
        memberships = await self._resolve_memberships(claims.subject_id)
        return Principal(
            subject_id=claims.subject_id,
            tenant_id=claims.tenant_id,
            platform_permissions=platform_permissions,
            memberships=memberships,
        )

    async def _resolve_memberships(
        self, subject_id: uuid.UUID
    ) -> dict[uuid.UUID, SellerGrant]:
        """Seller authority for the authenticated user (ADR-0011 §7, §10).

        Seller identity comes ONLY from Merchant's membership rows. `subject_id`
        is used purely to ask "which sellers is this user a member of" — it is
        never itself treated as a seller id, so a user cannot become a seller by
        existing.

        Every step fails closed:
          * no MembershipQuery wired      -> no seller authority
          * membership row absent         -> no grant
          * seller state absent/unknown   -> no grant
          * seller not ACTIVE             -> grant carries the real status, and
                                             SellerGrant.allows() denies on it
          * member role code unknown      -> dropped and logged, never guessed

        The result is a complete snapshot: a policy answers a seller-scoped
        question from it with no I/O of its own.
        """
        if self._memberships is None:
            return {}

        by_seller = await self._memberships.memberships_for(subject_id)
        if not by_seller:
            return {}

        states = await self._memberships.seller_states(list(by_seller))

        grants: dict[uuid.UUID, SellerGrant] = {}
        unknown_roles: list[str] = []
        for seller_id, member_role_code in by_seller.items():
            permissions = permissions_for(member_role_code)
            if permissions is None:
                unknown_roles.append(member_role_code)
                continue
            state = states.get(seller_id)
            if state is None:
                # No authorization state means nothing is known about the
                # seller, so nothing is conferred.
                continue
            grants[seller_id] = SellerGrant(
                seller_id=seller_id,
                permissions=permissions,
                status=state.status,
                tenant_id=state.tenant_id,   # tenancy is the SELLER's, not the user's
            )

        if unknown_roles:
            logger.warning(
                "dropping %d membership(s) with unknown member role code(s): %s",
                len(unknown_roles), sorted(set(unknown_roles)),
            )
        return grants

    async def _resolve_platform_permissions(
        self, role_codes: tuple[str, ...]
    ) -> frozenset[Permission]:
        """Role codes -> permission codes (DB) -> Permission members (enum).

        The parse is where the code-first catalogue meets the database, and it
        FAILS CLOSED: a code the enum does not know is dropped and logged, never
        granted. That is what makes a database ahead of its deployed code safe —
        the extra permission simply does not exist as far as this process is
        concerned (ADR-0011 §5).
        """
        codes = await self._role_grants.permission_codes_for_roles(role_codes)
        resolved: set[Permission] = set()
        unknown: list[str] = []
        for code in codes:
            permission = from_code(code)
            if permission is None:
                unknown.append(code)
            else:
                resolved.add(permission)
        if unknown:
            # Drift signal: the database holds codes this build does not define.
            logger.warning(
                "dropping %d unknown permission code(s) from role grants: %s",
                len(unknown),
                sorted(unknown),
            )
        return frozenset(resolved)
