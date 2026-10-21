"""Seller member role → seller-scoped permission matrix (ADR-0011 §7.3).

Merchant owns this. It is the seller-side counterpart of Identity's platform RBAC
baseline, and the two are deliberately separate systems:

    Identity   user_roles   -> PlatformRole      -> platform-scoped Permission
    Merchant   SellerMember -> SellerMemberRole  -> seller-scoped Permission

A permission reached through this matrix can only ever be exercised **within one
seller**, via `Principal.can_for_seller`. It never enters
`Principal.platform_permissions`, so it cannot become global authority.

Code-first and DB-free by construction: this module imports no infrastructure,
performs no I/O, and is not stored in the platform `roles` table. `validate()`
enforces the invariants, and the tests call it, so a drifting matrix fails the
build rather than silently changing who may reprice goods.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.modules.merchant.domain.entities import SellerMemberRole
from src.shared_kernel.application.permissions import Permission, PermissionScope

P = Permission
_R = SellerMemberRole

# The matrix approved for Phase 1F. Read it as: what may a member holding this
# role do inside the seller they are a member of?
#
#   owner   - full seller-scoped authority, including member management and
#             seller profile updates.
#   manager - commercial/operator authority: the full offer lifecycle including
#             price, plus stock. NOT member management, NOT seller profile.
#   staff   - day-to-day operations: create offers, receive stock, read.
#             NOT price changes, NOT offer activation/suspension.
#   viewer  - read-only. No mutation of any kind.
MEMBER_ROLE_PERMISSIONS: Mapping[SellerMemberRole, frozenset[Permission]] = {
    _R.OWNER: frozenset({
        P.MERCHANT_SELLERS_READ_OWN,
        P.MERCHANT_MEMBERS_READ,
        P.MERCHANT_SELLERS_UPDATE,
        P.MERCHANT_MEMBERS_INVITE,
        P.MERCHANT_MEMBERS_REMOVE,
        P.SELLING_OFFERS_READ_OWN,
        P.SELLING_OFFERS_CREATE,
        P.SELLING_OFFERS_ACTIVATE,
        P.SELLING_OFFERS_SUSPEND,
        P.SELLING_OFFERS_PRICE_CHANGE,
        P.INVENTORY_STOCK_READ_OWN,
        P.INVENTORY_STOCK_RECEIVE,
        P.INVENTORY_RESERVATIONS_READ,
    }),
    _R.MANAGER: frozenset({
        P.MERCHANT_SELLERS_READ_OWN,
        P.MERCHANT_MEMBERS_READ,
        P.SELLING_OFFERS_READ_OWN,
        P.SELLING_OFFERS_CREATE,
        P.SELLING_OFFERS_ACTIVATE,
        P.SELLING_OFFERS_SUSPEND,
        P.SELLING_OFFERS_PRICE_CHANGE,
        P.INVENTORY_STOCK_READ_OWN,
        P.INVENTORY_STOCK_RECEIVE,
        P.INVENTORY_RESERVATIONS_READ,
    }),
    _R.STAFF: frozenset({
        P.MERCHANT_SELLERS_READ_OWN,
        P.SELLING_OFFERS_READ_OWN,
        P.SELLING_OFFERS_CREATE,
        P.INVENTORY_STOCK_READ_OWN,
        P.INVENTORY_STOCK_RECEIVE,
        P.INVENTORY_RESERVATIONS_READ,
    }),
    _R.VIEWER: frozenset({
        P.MERCHANT_SELLERS_READ_OWN,
        P.MERCHANT_MEMBERS_READ,
        P.SELLING_OFFERS_READ_OWN,
        P.INVENTORY_STOCK_READ_OWN,
        P.INVENTORY_RESERVATIONS_READ,
    }),
}

# Approved sizes, asserted by `validate()` so an accidental edit is caught even
# if it happens to keep every other invariant intact.
APPROVED_SIZES: Mapping[SellerMemberRole, int] = {
    _R.OWNER: 13, _R.MANAGER: 10, _R.STAFF: 6, _R.VIEWER: 5,
}

# Mutations a viewer must never hold. Kept explicit rather than derived from a
# name convention, because "which permission mutates" is a policy statement.
_MUTATIONS: frozenset[Permission] = frozenset({
    P.MERCHANT_SELLERS_UPDATE,
    P.MERCHANT_MEMBERS_INVITE,
    P.MERCHANT_MEMBERS_REMOVE,
    P.SELLING_OFFERS_CREATE,
    P.SELLING_OFFERS_ACTIVATE,
    P.SELLING_OFFERS_SUSPEND,
    P.SELLING_OFFERS_PRICE_CHANGE,
    P.INVENTORY_STOCK_RECEIVE,
})


class MemberPermissionError(Exception):
    """The matrix violates an approved invariant — a programming error."""


def validate() -> None:
    """Fail loudly on any drift from the approved policy."""
    missing = set(SellerMemberRole) - set(MEMBER_ROLE_PERMISSIONS)
    if missing:
        raise MemberPermissionError(
            f"member roles with no mapping: {sorted(r.value for r in missing)}"
        )

    for role, permissions in MEMBER_ROLE_PERMISSIONS.items():
        platform = sorted(
            p.code for p in permissions if p.scope is not PermissionScope.SELLER_SCOPABLE
        )
        if platform:
            raise MemberPermissionError(
                f"member role {role.value!r} is granted platform-scoped "
                f"permission(s) {platform}; platform authority comes from a "
                f"platform role, never from a seller membership"
            )
        if len(permissions) != APPROVED_SIZES[role]:
            raise MemberPermissionError(
                f"member role {role.value!r} has {len(permissions)} permissions; "
                f"the approved matrix grants {APPROVED_SIZES[role]}"
            )

    # The manager/staff boundary — decisions A, B and C of the approval.
    staff = MEMBER_ROLE_PERMISSIONS[_R.STAFF]
    manager = MEMBER_ROLE_PERMISSIONS[_R.MANAGER]
    owner = MEMBER_ROLE_PERMISSIONS[_R.OWNER]

    for forbidden in (P.SELLING_OFFERS_PRICE_CHANGE, P.SELLING_OFFERS_ACTIVATE,
                      P.SELLING_OFFERS_SUSPEND):
        if forbidden in staff:
            raise MemberPermissionError(
                f"seller_staff must not hold {forbidden.code}"
            )
    for required in (P.SELLING_OFFERS_PRICE_CHANGE, P.SELLING_OFFERS_ACTIVATE,
                     P.SELLING_OFFERS_SUSPEND):
        if required not in manager:
            raise MemberPermissionError(f"seller_manager must hold {required.code}")

    for owner_only in (P.MERCHANT_MEMBERS_INVITE, P.MERCHANT_MEMBERS_REMOVE,
                       P.MERCHANT_SELLERS_UPDATE):
        if owner_only not in owner:
            raise MemberPermissionError(f"seller_owner must hold {owner_only.code}")
        for role in (_R.MANAGER, _R.STAFF, _R.VIEWER):
            if owner_only in MEMBER_ROLE_PERMISSIONS[role]:
                raise MemberPermissionError(
                    f"{role.value} must not hold {owner_only.code}; it is owner-only"
                )

    if MEMBER_ROLE_PERMISSIONS[_R.VIEWER] & _MUTATIONS:
        raise MemberPermissionError("seller_viewer must hold no mutation permission")


def permissions_for(member_role_code: str) -> frozenset[Permission] | None:
    """Map a database member-role code to its permissions.

    Returns None for a code this build does not define, so the caller drops the
    membership. Fail-closed: an unrecognised role must never confer authority,
    and must never be silently treated as a known one.
    """
    try:
        role = SellerMemberRole(member_role_code)
    except ValueError:
        return None
    return MEMBER_ROLE_PERMISSIONS.get(role)
