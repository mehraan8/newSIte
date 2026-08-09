"""Principal and the authorization ports (ADR-0011 §4, §5, §10).

What lives here is the *vocabulary* of authorization: who the caller is and what
they may do. What does NOT live here is any decision about a particular object —
that is a per-context Policy's job (ADR-0011 principle 5, no central authorization
service).

Dependency rule: this module imports nothing from any bounded context. Identity
issues tokens, Merchant owns memberships, and both are reached through the ports
below, wired at the composition root. That is what lets the Shared Kernel become
a shared library on extraction without dragging a context along with it.

Principal is an EXPLICIT parameter everywhere (ADR-0011 principle 4). It is never
a ContextVar, never thread-local, never ambient. `EventContext` in this package is
ambient on purpose — it must be impossible to *forget*. Principal is the opposite:
it must be impossible to *omit silently*, so a missing authorization check shows
up as a signature change in review rather than an invisible default.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from src.shared_kernel.application.permissions import Permission


class SellerStatus(str, Enum):
    """The authorization-relevant view of a seller's lifecycle.

    Deliberately declared here rather than imported from Merchant: the Shared
    Kernel may not depend on a bounded context. Merchant owns the authoritative
    status and maps its domain value onto this one at the port boundary.

    Only ACTIVE confers authority. A suspended seller's members keep their
    membership rows and may keep a warm membership cache entry, and must still be
    denied — see ADR-0011 §10.8, case I.
    """

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"

    @property
    def grants_authority(self) -> bool:
        return self is SellerStatus.ACTIVE


@dataclass(frozen=True)
class SellerGrant:
    """What a principal may do *within one seller*.

    Carries the seller's status so that a policy can decide a seller-scoped
    question entirely from the Principal, with no I/O (ADR-0011 §10.3). Seller
    status is resolved during Principal assembly, never looked up by a policy.
    """

    seller_id: uuid.UUID
    permissions: frozenset[Permission]
    status: SellerStatus
    tenant_id: uuid.UUID | None = None

    def allows(self, permission: Permission) -> bool:
        """True only if the seller is active AND the grant carries the permission."""
        return self.status.grants_authority and permission in self.permissions


@dataclass(frozen=True)
class Principal:
    """The authenticated caller's capability set — nothing more.

    Deliberately absent: mobile, email, display name, the token string, request
    metadata, IP, locale. If a policy needs a user attribute, the model is wrong.
    """

    subject_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    platform_permissions: frozenset[Permission] = frozenset()
    memberships: Mapping[uuid.UUID, SellerGrant] = field(default_factory=dict)

    # -- accessors ----------------------------------------------------------
    def can(self, permission: Permission) -> bool:
        """Holds `permission` platform-wide (granted through a platform role)."""
        return permission in self.platform_permissions

    def can_for_seller(self, permission: Permission, seller_id: uuid.UUID) -> bool:
        """Holds `permission` *within that seller*, through a SellerMember role.

        Strictly membership-scoped: a platform-wide grant does NOT satisfy this.
        Platform overrides (`selling.offers.manage.any`, `inventory.manage.any`)
        are checked explicitly by the policy that honours them, e.g.

            principal.can(Permission.SELLING_OFFERS_MANAGE_ANY) \\
                or principal.can_for_seller(Permission.SELLING_OFFERS_ACTIVATE, sid)

        Keeping the override at the policy site rather than hiding it in a
        code->override mapping means every widening of authority is visible where
        it is granted, and `can_for_seller` cannot silently answer "yes" for a
        seller the caller has no membership in.
        """
        grant = self.memberships.get(seller_id)
        return grant is not None and grant.allows(permission)

    @property
    def seller_ids(self) -> frozenset[uuid.UUID]:
        """Sellers this principal is a member of, whatever their status."""
        return frozenset(self.memberships)

    def is_member_of(self, seller_id: uuid.UUID) -> bool:
        return seller_id in self.memberships


# --- Ports -----------------------------------------------------------------
# Defined here, implemented by infrastructure, wired at the composition root.
# Dependency inversion: the Shared Kernel depends on neither Identity nor
# Merchant, so extraction moves an adapter and nothing else.


@dataclass(frozen=True)
class SellerAuthorizationState:
    """The security-relevant projection of a Seller (ADR-0011 §10.2).

    Exactly the four fields the cache holds: no name, no commercial attributes,
    no PII. `version` is monotonic and is what makes out-of-order event delivery
    safe — an older version is discarded rather than applied.
    """

    seller_id: uuid.UUID
    status: SellerStatus
    tenant_id: uuid.UUID | None
    version: int


@runtime_checkable
class RoleGrantsQuery(Protocol):
    """"Which permission codes does this role code grant?" — and nothing else.

    Returns raw DB strings: this is one of the two ADR-approved string boundaries.
    Principal assembly parses them into `Permission` members and drops unknown
    codes (fail-closed). The catalogue of what permissions *exist* is the enum,
    never this query.
    """

    async def permission_codes_for_roles(
        self, role_codes: Sequence[str]
    ) -> frozenset[str]:
        ...


@runtime_checkable
class MembershipQuery(Protocol):
    """Seller memberships and seller authorization state.

    The implementation is cache-first with bounded database fallback (ADR-0011
    §10.7). Principal and policies must not know, and must not be able to tell,
    whether an answer came from Redis or PostgreSQL.
    """

    async def memberships_for(self, user_id: uuid.UUID) -> Mapping[uuid.UUID, str]:
        """seller_id -> member_role_code. An empty mapping is a real answer and
        MUST be cacheable (ADR-0011 §10.1, negative caching)."""
        ...

    async def seller_states(
        self, seller_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, SellerAuthorizationState]:
        """Authorization state for the given sellers. Sellers with no state are
        omitted; the caller treats a missing entry as conferring no authority."""
        ...
