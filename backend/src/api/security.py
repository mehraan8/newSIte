"""Principal assembly — the composition root's authorization seam (ADR-0011 §3).

The four-layer model is: authentication -> PRINCIPAL ASSEMBLY -> router coarse
check -> application policy. This module is the second layer, and the only place
where authorization state is built.

Everything that costs I/O happens here, once per request, so policies downstream
stay pure and synchronous (ADR-0011 §9). Placing assembly behind a factory is
also what makes the membership source swappable — Phase 1F replaces the absent
MembershipQuery with the Redis-backed adapter and nothing here or downstream
changes shape.

Principal is EXPLICIT (ADR-0011 principle 4): this module returns one, and
callers pass it down. It is never stashed in a ContextVar, module global, or
request attribute.

SCOPE (Phase 1B): assembly exists and is tested. It is NOT yet attached to any
route — route classification and enforcement are Phase 1I/1K, and seller
memberships arrive with Phase 1C/1F. Until then `memberships` is empty, which is
fail-closed: `can_for_seller` answers False for every seller.
"""

from __future__ import annotations

import logging

from src.modules.identity.application.ports import AccessTokenClaims
from src.shared_kernel.application.permissions import Permission, from_code
from src.shared_kernel.application.security import (
    MembershipQuery,
    Principal,
    RoleGrantsQuery,
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
        # Optional strictly because Merchant (Phase 1C) and the membership cache
        # (Phase 1F) do not exist yet. Absent means "no memberships", which denies
        # every seller-scoped question rather than allowing it.
        self._memberships = memberships

    async def from_claims(self, claims: AccessTokenClaims) -> Principal:
        platform_permissions = await self._resolve_platform_permissions(
            claims.role_codes
        )
        return Principal(
            subject_id=claims.subject_id,
            tenant_id=claims.tenant_id,
            platform_permissions=platform_permissions,
            memberships={},  # Phase 1C/1F
        )

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
