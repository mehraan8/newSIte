"""The approved seller member-role → permission matrix (Phase 1F). DB-free.

The matrix decides who may reprice goods and who may hand out access to a
merchant account, so every approved cell is asserted individually rather than
by a summary count alone.
"""

from __future__ import annotations

import pytest

from src.modules.merchant.application.member_permissions import (
    APPROVED_SIZES,
    MEMBER_ROLE_PERMISSIONS,
    MemberPermissionError,
    permissions_for,
    validate,
)
from src.modules.merchant.domain.entities import SellerMemberRole as R
from src.modules.identity.application.rbac_baseline import ROLE_PERMISSIONS
from src.shared_kernel.application.permissions import Permission as P
from src.shared_kernel.application.permissions import PermissionScope

# The approved matrix, transcribed independently of the implementation so the
# test cannot pass merely because it imports the same table it is checking.
APPROVED = {
    "merchant.sellers.read.own":   {"owner", "manager", "staff", "viewer"},
    "merchant.members.read":       {"owner", "manager", "viewer"},
    "merchant.sellers.update":     {"owner"},
    "merchant.members.invite":     {"owner"},
    "merchant.members.remove":     {"owner"},
    "selling.offers.read.own":     {"owner", "manager", "staff", "viewer"},
    "selling.offers.create":       {"owner", "manager", "staff"},
    "selling.offers.activate":     {"owner", "manager"},
    "selling.offers.suspend":      {"owner", "manager"},
    "selling.offers.price.change": {"owner", "manager"},
    "inventory.stock.read.own":    {"owner", "manager", "staff", "viewer"},
    "inventory.stock.receive":     {"owner", "manager", "staff"},
    "inventory.reservations.read": {"owner", "manager", "staff", "viewer"},
}
BY_NAME = {"owner": R.OWNER, "manager": R.MANAGER, "staff": R.STAFF, "viewer": R.VIEWER}


def test_matrix_validates():
    validate()


@pytest.mark.parametrize(("code", "holders"), sorted(APPROVED.items()))
def test_every_permission_is_granted_to_exactly_the_approved_roles(code, holders):
    for name, role in BY_NAME.items():
        granted = any(p.code == code for p in MEMBER_ROLE_PERMISSIONS[role])
        assert granted is (name in holders), (
            f"{code}: {name} should {'hold' if name in holders else 'NOT hold'} it"
        )


def test_approved_totals():
    assert {r: len(p) for r, p in MEMBER_ROLE_PERMISSIONS.items()} == dict(APPROVED_SIZES)
    assert APPROVED_SIZES == {R.OWNER: 13, R.MANAGER: 10, R.STAFF: 6, R.VIEWER: 5}


# --- decision A: staff may not change prices --------------------------------
def test_staff_cannot_change_price():
    assert P.SELLING_OFFERS_PRICE_CHANGE not in MEMBER_ROLE_PERMISSIONS[R.STAFF]


def test_manager_can_change_price():
    assert P.SELLING_OFFERS_PRICE_CHANGE in MEMBER_ROLE_PERMISSIONS[R.MANAGER]


# --- decision B: offer lifecycle is commercial, not day-to-day --------------
def test_staff_cannot_activate_or_suspend_offers():
    staff = MEMBER_ROLE_PERMISSIONS[R.STAFF]
    assert P.SELLING_OFFERS_ACTIVATE not in staff
    assert P.SELLING_OFFERS_SUSPEND not in staff


def test_manager_can_activate_and_suspend_offers():
    manager = MEMBER_ROLE_PERMISSIONS[R.MANAGER]
    assert P.SELLING_OFFERS_ACTIVATE in manager
    assert P.SELLING_OFFERS_SUSPEND in manager


def test_staff_can_still_create_offers_and_receive_stock():
    staff = MEMBER_ROLE_PERMISSIONS[R.STAFF]
    assert P.SELLING_OFFERS_CREATE in staff
    assert P.INVENTORY_STOCK_RECEIVE in staff


# --- decision C: member management is owner-only ----------------------------
def test_only_owner_manages_members_and_seller_profile():
    for owner_only in (P.MERCHANT_MEMBERS_INVITE, P.MERCHANT_MEMBERS_REMOVE,
                       P.MERCHANT_SELLERS_UPDATE):
        assert owner_only in MEMBER_ROLE_PERMISSIONS[R.OWNER]
        for role in (R.MANAGER, R.STAFF, R.VIEWER):
            assert owner_only not in MEMBER_ROLE_PERMISSIONS[role]


# --- decision D / viewer ----------------------------------------------------
def test_owner_holds_the_complete_seller_scoped_set():
    scopable = {p for p in P if p.is_seller_scopable}
    assert MEMBER_ROLE_PERMISSIONS[R.OWNER] == scopable


def test_viewer_holds_no_mutation_permission():
    mutations = {
        P.MERCHANT_SELLERS_UPDATE, P.MERCHANT_MEMBERS_INVITE, P.MERCHANT_MEMBERS_REMOVE,
        P.SELLING_OFFERS_CREATE, P.SELLING_OFFERS_ACTIVATE, P.SELLING_OFFERS_SUSPEND,
        P.SELLING_OFFERS_PRICE_CHANGE, P.INVENTORY_STOCK_RECEIVE,
    }
    assert not (MEMBER_ROLE_PERMISSIONS[R.VIEWER] & mutations)


# --- decisions E / F: the two vocabularies stay separate --------------------
def test_no_member_role_holds_a_platform_scoped_permission():
    for role, permissions in MEMBER_ROLE_PERMISSIONS.items():
        offenders = sorted(
            p.code for p in permissions if p.scope is not PermissionScope.SELLER_SCOPABLE
        )
        assert offenders == [], f"{role.value} holds platform-scoped {offenders}"


def test_no_platform_role_holds_a_seller_scoped_permission():
    """The Phase 1E invariant, restated from the Merchant side."""
    for role, permissions in ROLE_PERMISSIONS.items():
        offenders = sorted(p.code for p in permissions if p.is_seller_scopable)
        assert offenders == [], f"{role.code} holds seller-scopable {offenders}"


def test_the_two_permission_universes_are_disjoint():
    member = {p for perms in MEMBER_ROLE_PERMISSIONS.values() for p in perms}
    platform = {p for perms in ROLE_PERMISSIONS.values() for p in perms}
    assert member.isdisjoint(platform)


# --- lookup fails closed ----------------------------------------------------
@pytest.mark.parametrize("role", list(R))
def test_every_declared_member_role_resolves(role):
    assert permissions_for(role.value) == MEMBER_ROLE_PERMISSIONS[role]


@pytest.mark.parametrize(
    "unknown", ["seller", "admin", "owner", "SELLER_OWNER", "", "wizard"]
)
def test_unknown_member_role_code_confers_nothing(unknown):
    assert permissions_for(unknown) is None


# --- the guard itself must be able to fail ----------------------------------
def test_validate_rejects_a_platform_scoped_permission():
    import src.modules.merchant.application.member_permissions as mod

    original = mod.MEMBER_ROLE_PERMISSIONS
    mod.MEMBER_ROLE_PERMISSIONS = {
        **original, R.VIEWER: original[R.VIEWER] | {P.OPS_DLQ_REPLAY}
    }
    try:
        with pytest.raises(MemberPermissionError, match="platform-scoped"):
            validate()
    finally:
        mod.MEMBER_ROLE_PERMISSIONS = original
    validate()


def test_validate_rejects_giving_staff_the_price_permission():
    """Decision A is protected by a guard that provably fires."""
    import src.modules.merchant.application.member_permissions as mod

    original = mod.MEMBER_ROLE_PERMISSIONS
    mod.MEMBER_ROLE_PERMISSIONS = {
        **original, R.STAFF: original[R.STAFF] | {P.SELLING_OFFERS_PRICE_CHANGE}
    }
    try:
        with pytest.raises(MemberPermissionError):
            validate()
    finally:
        mod.MEMBER_ROLE_PERMISSIONS = original


def test_validate_rejects_giving_a_manager_member_management():
    """Swaps rather than adds, so the permission COUNT stays at the approved 10
    and the owner-only rule is the only check that can reject it."""
    import src.modules.merchant.application.member_permissions as mod

    original = mod.MEMBER_ROLE_PERMISSIONS
    smuggled = (
        original[R.MANAGER] - {P.MERCHANT_MEMBERS_READ}
    ) | {P.MERCHANT_MEMBERS_INVITE}
    assert len(smuggled) == APPROVED_SIZES[R.MANAGER]
    mod.MEMBER_ROLE_PERMISSIONS = {**original, R.MANAGER: smuggled}
    try:
        with pytest.raises(MemberPermissionError, match="owner-only"):
            validate()
    finally:
        mod.MEMBER_ROLE_PERMISSIONS = original


def test_validate_rejects_a_missing_role_mapping():
    import src.modules.merchant.application.member_permissions as mod

    original = mod.MEMBER_ROLE_PERMISSIONS
    mod.MEMBER_ROLE_PERMISSIONS = {r: p for r, p in original.items() if r is not R.VIEWER}
    try:
        with pytest.raises(MemberPermissionError, match="no mapping"):
            validate()
    finally:
        mod.MEMBER_ROLE_PERMISSIONS = original


def test_matrix_module_performs_no_io():
    """Code-first and DB-free: no infrastructure import, no session, no redis."""
    import ast
    import pathlib

    path = (pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" /
            "merchant" / "application" / "member_permissions.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for name in names:
            assert "infrastructure" not in name, name
            assert not name.startswith(("sqlalchemy", "redis", "asyncpg")), name
