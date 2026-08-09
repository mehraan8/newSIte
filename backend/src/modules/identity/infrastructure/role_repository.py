"""Read adapters over the RBAC grant tables (ADR-0011 §5.1).

Two distinct questions, two adapters:

  * SqlPlatformRoleQuery  - "which platform ROLES does this user hold?"
    Read at token ISSUANCE only. Identity owns `user_roles`.

  * SqlRoleGrantsQuery    - "which permission CODES does this role grant?"
    Read during Principal assembly. Satisfies the Shared Kernel port of the same
    name, so the composition root can wire it without the Shared Kernel ever
    importing Identity.

Narrow SQL rather than ORM, matching the existing gateway adapters
(SqlSellingGateway, SqlCatalogVariantGateway). ORM models for the four RBAC
tables are Phase 1D; when they land, these method bodies change and the ports do
not.

`permissions` is queried here only to translate `role_permissions.permission_id`
into a CODE. It is never asked what permissions EXIST — that is the `Permission`
enum's job (ADR-0011 §5), and this adapter returning a code the enum does not
know results in that code being dropped, not in a new permission appearing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ROLE_CODES_FOR_USER = text(
    """
    SELECT r.code
      FROM user_roles ur
      JOIN roles r ON r.id = ur.role_id
     WHERE ur.user_id = :user_id
     ORDER BY r.code
    """
)

# Joined on role CODE, never on a surrogate id: ids differ per environment
# (ADR-0011 §12).
_PERMISSION_CODES_FOR_ROLES = text(
    """
    SELECT DISTINCT p.code
      FROM roles r
      JOIN role_permissions rp ON rp.role_id = r.id
      JOIN permissions p ON p.id = rp.permission_id
     WHERE r.code = ANY(:role_codes)
    """
)


class SqlPlatformRoleQuery:
    """Implements the Identity `PlatformRoleQuery` port."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def role_codes_for_user(self, user_id: uuid.UUID) -> tuple[str, ...]:
        rows = await self._s.execute(_ROLE_CODES_FOR_USER, {"user_id": str(user_id)})
        return tuple(row[0] for row in rows)


class SqlRoleGrantsQuery:
    """Implements the Shared Kernel `RoleGrantsQuery` port.

    Returns raw database strings — one of the two ADR-approved string boundaries.
    The caller parses them into `Permission` members and drops what it does not
    recognise.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def permission_codes_for_roles(
        self, role_codes: Sequence[str]
    ) -> frozenset[str]:
        if not role_codes:
            # No roles means no platform permissions. Skipping the query keeps the
            # common case (a customer with no platform grants) free of I/O.
            return frozenset()
        rows = await self._s.execute(
            _PERMISSION_CODES_FOR_ROLES, {"role_codes": list(role_codes)}
        )
        return frozenset(row[0] for row in rows)
