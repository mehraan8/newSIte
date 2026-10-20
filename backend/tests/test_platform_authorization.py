"""Platform back-office authorization (DB-free) — Phase 1H.

Two things are proved here that a "does the admin work?" test never reaches:

  * NEGATIVE SPACE. The absence of authority is part of the contract. Every role
    is tested against every operation it must NOT have, not just the ones it must.
  * PERMISSION INDEPENDENCE. `update` must not authorize `publish`; `publish`
    must not authorize `archive`; `manage.any` must not authorize a reservation
    commit. Permissions that quietly imply each other are a matrix that lies.

The role fixtures are built from the Phase 1E baseline itself, never hand-picked,
so a change to the baseline surfaces here instead of silently passing.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.modules.identity.application.rbac_baseline import ROLE_PERMISSIONS
from src.modules.identity.application.roles import PlatformRole
from src.shared_kernel.application.errors import ForbiddenError
from src.shared_kernel.application.permissions import Permission, PermissionScope
from src.shared_kernel.application.platform_policy import ensure_can_platform
from src.shared_kernel.application.security import Principal, SellerGrant, SellerStatus

ALL_ROLES = list(PlatformRole)

# The back-office operations Phase 1H governs, and the permission each demands.
CATALOG_OPS = {
    "create product": (Permission.CATALOG_PRODUCTS_CREATE,),
    "edit attributes": (Permission.CATALOG_PRODUCTS_UPDATE,),
    "publish": (Permission.CATALOG_PRODUCTS_PUBLISH,),
    "archive": (Permission.CATALOG_PRODUCTS_ARCHIVE,),
    "edit and publish": (
        Permission.CATALOG_PRODUCTS_UPDATE,
        Permission.CATALOG_PRODUCTS_PUBLISH,
    ),
}
INVENTORY_OPS = {
    "create reservation": (Permission.INVENTORY_RESERVATIONS_CREATE,),
    "commit reservation": (Permission.INVENTORY_RESERVATIONS_COMMIT,),
    "release reservation": (Permission.INVENTORY_RESERVATIONS_RELEASE,),
}
ALL_OPS = {**CATALOG_OPS, **INVENTORY_OPS}


def as_role(role: PlatformRole) -> Principal:
    """A principal holding exactly what the seeded baseline grants that role."""
    return Principal(
        subject_id=uuid.uuid4(),
        platform_permissions=frozenset(ROLE_PERMISSIONS[role]),
    )


def holding(*permissions: Permission) -> Principal:
    return Principal(subject_id=uuid.uuid4(), platform_permissions=frozenset(permissions))


def allowed(principal: Principal, permissions) -> bool:
    try:
        ensure_can_platform(principal, *permissions)
        return True
    except ForbiddenError:
        return False


# ===========================================================================
# The full role × operation matrix, derived from the baseline
# ===========================================================================
@pytest.mark.parametrize("role", ALL_ROLES, ids=lambda r: r.code)
@pytest.mark.parametrize("operation", list(ALL_OPS), ids=lambda o: o.replace(" ", "_"))
def test_every_role_against_every_back_office_operation(role, operation):
    """The whole matrix, both directions. The expectation is computed from the
    baseline rather than written down again, so this cannot drift from the seed
    — and cannot be "fixed" by editing an expectation to match a bug."""
    required = ALL_OPS[operation]
    expected = all(p in ROLE_PERMISSIONS[role] for p in required)

    assert allowed(as_role(role), required) is expected, (
        f"{role.code} vs {operation!r}: expected "
        f"{'allow' if expected else 'deny'}"
    )


@pytest.mark.parametrize("operation", list(ALL_OPS), ids=lambda o: o.replace(" ", "_"))
def test_a_customer_reaches_no_back_office_operation(operation):
    assert not allowed(as_role(PlatformRole.CUSTOMER), ALL_OPS[operation])


@pytest.mark.parametrize("operation", list(ALL_OPS), ids=lambda o: o.replace(" ", "_"))
def test_readonly_operator_can_mutate_nothing(operation):
    """Its whole purpose is observability with zero write capability. If this
    ever passes for a mutation, the role has stopped meaning anything."""
    assert not allowed(as_role(PlatformRole.READONLY_OPERATOR), ALL_OPS[operation])


@pytest.mark.parametrize("operation", list(ALL_OPS), ids=lambda o: o.replace(" ", "_"))
def test_support_agent_reaches_no_catalog_or_inventory_mutation(operation):
    assert not allowed(as_role(PlatformRole.SUPPORT_AGENT), ALL_OPS[operation])


@pytest.mark.parametrize("operation", list(INVENTORY_OPS), ids=lambda o: o.replace(" ", "_"))
def test_catalog_admin_gains_no_inventory_authority(operation):
    """Roles are not hierarchical and not cumulative."""
    assert not allowed(as_role(PlatformRole.CATALOG_ADMIN), INVENTORY_OPS[operation])


@pytest.mark.parametrize("operation", list(CATALOG_OPS), ids=lambda o: o.replace(" ", "_"))
def test_inventory_manager_gains_no_catalog_authority(operation):
    assert not allowed(as_role(PlatformRole.INVENTORY_MANAGER), CATALOG_OPS[operation])


@pytest.mark.parametrize("operation", list(ALL_OPS), ids=lambda o: o.replace(" ", "_"))
def test_super_admin_is_allowed_only_through_real_permissions(operation):
    """No `if super_admin: allow()` anywhere. It passes because the seeded
    baseline actually grants it these, which this asserts explicitly."""
    required = ALL_OPS[operation]
    for permission in required:
        assert permission in ROLE_PERMISSIONS[PlatformRole.SUPER_ADMIN]
    assert allowed(as_role(PlatformRole.SUPER_ADMIN), required)


# ===========================================================================
# Cross-permission confusion — each permission authorizes ONLY its operation
# ===========================================================================
CONFUSION_PAIRS = [
    (Permission.CATALOG_PRODUCTS_CREATE, Permission.CATALOG_PRODUCTS_UPDATE),
    (Permission.CATALOG_PRODUCTS_UPDATE, Permission.CATALOG_PRODUCTS_PUBLISH),
    (Permission.CATALOG_PRODUCTS_PUBLISH, Permission.CATALOG_PRODUCTS_ARCHIVE),
    (Permission.CATALOG_PRODUCTS_ARCHIVE, Permission.CATALOG_PRODUCTS_UPDATE),
    (Permission.INVENTORY_STOCK_READ_ANY, Permission.INVENTORY_MANAGE_ANY),
    (Permission.INVENTORY_MANAGE_ANY, Permission.INVENTORY_RESERVATIONS_COMMIT),
    (Permission.INVENTORY_RESERVATIONS_CREATE, Permission.INVENTORY_RESERVATIONS_COMMIT),
    (Permission.INVENTORY_RESERVATIONS_COMMIT, Permission.INVENTORY_RESERVATIONS_RELEASE),
    (Permission.INVENTORY_RESERVATIONS_RELEASE, Permission.INVENTORY_RESERVATIONS_CREATE),
]


@pytest.mark.parametrize(
    ("held", "attempted"), CONFUSION_PAIRS,
    ids=[f"{h.code}~{a.code}" for h, a in CONFUSION_PAIRS],
)
def test_one_permission_never_authorizes_a_neighbouring_operation(held, attempted):
    """Holding one is not holding the other. These pairs are the ones most
    likely to be conflated by a well-meaning refactor — `manage.any` reading as
    "inventory admin", `update` reading as "may change anything about it"."""
    assert allowed(holding(held), (held,))
    assert not allowed(holding(held), (attempted,))


def test_a_compound_operation_needs_every_permission_not_any():
    """Editing and publishing in one request needs both. `any` semantics would
    let an edit-only role publish by attaching a name change."""
    both = (Permission.CATALOG_PRODUCTS_UPDATE, Permission.CATALOG_PRODUCTS_PUBLISH)

    assert not allowed(holding(Permission.CATALOG_PRODUCTS_UPDATE), both)
    assert not allowed(holding(Permission.CATALOG_PRODUCTS_PUBLISH), both)
    assert allowed(holding(*both), both)


# ===========================================================================
# Platform / seller separation
# ===========================================================================
def test_a_seller_membership_never_satisfies_a_platform_check():
    """The hard boundary. A seller owner holds seller-scoped permissions with the
    same NAMES in some cases; they live in a different universe and must not
    cross."""
    seller = uuid.uuid4()
    member = Principal(
        subject_id=uuid.uuid4(),
        memberships={
            seller: SellerGrant(
                seller_id=seller,
                permissions=frozenset(Permission),  # everything, deliberately
                status=SellerStatus.ACTIVE,
            )
        },
    )

    for required in ALL_OPS.values():
        assert not allowed(member, required)


def test_no_platform_role_holds_a_seller_scoped_permission():
    """The baseline itself must respect the split, or the policy above is
    guarding a door in a wall that has a hole in it."""
    offenders = []
    for role, permissions in ROLE_PERMISSIONS.items():
        for permission in permissions:
            if permission.scope is PermissionScope.SELLER_SCOPABLE:
                offenders.append(f"{role.code} holds {permission.code}")
    assert offenders == [], offenders


def test_the_policy_reads_platform_permissions_only():
    """Structural: `ensure_can_platform` must go through `Principal.can`, which
    reads `platform_permissions` and nothing else."""
    import inspect

    source = inspect.getsource(ensure_can_platform)
    assert "principal.can(" in source
    for forbidden in ("memberships", "can_for_seller", "seller"):
        assert forbidden not in source, f"platform policy references {forbidden!r}"


# ===========================================================================
# Shape of the policy
# ===========================================================================
def test_the_policy_is_pure_and_synchronous():
    assert not asyncio.iscoroutinefunction(ensure_can_platform)


def test_every_denial_reads_the_same():
    """The message must not name the missing permission — that would let a caller
    map the authorization model one request at a time."""
    messages = set()
    for required in ALL_OPS.values():
        with pytest.raises(ForbiddenError) as exc:
            ensure_can_platform(as_role(PlatformRole.CUSTOMER), *required)
        messages.add(str(exc.value))

    assert len(messages) == 1, messages
    only_message = messages.pop()
    for permission in Permission:
        assert permission.code not in only_message, permission.code


def test_the_denial_names_no_permission():
    with pytest.raises(ForbiddenError) as exc:
        ensure_can_platform(holding(), Permission.CATALOG_PRODUCTS_CREATE)
    assert Permission.CATALOG_PRODUCTS_CREATE.code not in str(exc.value)


def test_calling_with_no_permission_is_a_programming_error():
    """A guard that requires nothing would admit everyone, silently. Fail loudly
    instead — this is the shape a refactor mistake takes."""
    with pytest.raises(ValueError):
        ensure_can_platform(holding())
