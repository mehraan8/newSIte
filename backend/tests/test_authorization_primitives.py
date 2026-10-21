"""Shared Kernel authorization primitives (ADR-0011 §4–§6, Phase 1A).

DB-free and framework-free by design: these are pure value objects, so the whole
suite is a truth table. Anything here that needed a database or a request would
mean the primitives had leaked I/O, which ADR-0011 forbids.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from src.shared_kernel.application.permissions import (
    BoundedContext,
    Permission,
    PermissionScope,
    all_codes,
    from_code,
)
from src.shared_kernel.application.security import (
    MembershipQuery,
    Principal,
    RoleGrantsQuery,
    SellerAuthorizationState,
    SellerGrant,
    SellerStatus,
)


# --- the catalogue ---------------------------------------------------------
def test_every_permission_code_is_unique():
    codes = [p.code for p in Permission]
    assert len(codes) == len(set(codes)), "duplicate permission codes"


def test_member_name_is_the_upper_snake_transform_of_its_code():
    """Keeps the enum mechanically diffable against the seeded table."""
    for permission in Permission:
        expected = permission.code.upper().replace(".", "_")
        assert permission.name == expected, (
            f"{permission.name} does not match its code {permission.code!r}"
        )


def test_codes_follow_the_context_resource_action_convention():
    for permission in Permission:
        parts = permission.code.split(".")
        assert len(parts) >= 3, f"{permission.code} is not <context>.<resource>.<action>"
        assert parts[0] == permission.context.value, (
            f"{permission.code} is owned by {permission.context.value}"
        )
        assert permission.code == permission.code.lower()


def test_every_permission_carries_complete_metadata():
    for permission in Permission:
        assert permission.description.strip(), f"{permission.name} has no description"
        assert isinstance(permission.context, BoundedContext)
        assert isinstance(permission.scope, PermissionScope)


def test_from_code_round_trips_every_member():
    for permission in Permission:
        assert from_code(permission.code) is permission


def test_unknown_code_fails_closed():
    """ADR-0011 §5: a code the enum does not know confers no authority."""
    assert from_code("selling.offers.price.chnage") is None  # typo, deliberately
    assert from_code("") is None
    assert from_code("totally.made.up") is None


def test_all_codes_matches_the_enum():
    assert all_codes() == {p.code for p in Permission}


def test_seller_scopable_permissions_are_marked_as_such():
    """`seller` is not a platform role (ADR-0011 §6): the permissions that carry
    selling authority must be seller-scopable, or `can_for_seller` can never
    answer them."""
    for permission in (
        Permission.SELLING_OFFERS_CREATE,
        Permission.SELLING_OFFERS_ACTIVATE,
        Permission.SELLING_OFFERS_SUSPEND,
        Permission.SELLING_OFFERS_PRICE_CHANGE,
        Permission.INVENTORY_STOCK_RECEIVE,
        Permission.MERCHANT_MEMBERS_INVITE,
    ):
        assert permission.is_seller_scopable, f"{permission.name} must be seller-scopable"


def test_platform_wide_overrides_are_not_seller_scopable():
    """An `.any` override is granted by a platform role, never within a seller."""
    for permission in (
        Permission.SELLING_OFFERS_MANAGE_ANY,
        Permission.INVENTORY_MANAGE_ANY,
        Permission.MERCHANT_SELLERS_MANAGE_ANY,
        Permission.ORDERING_ORDERS_CANCEL_ANY,
    ):
        assert permission.scope is PermissionScope.PLATFORM


def test_str_is_the_code():
    assert str(Permission.CATALOG_PRODUCTS_CREATE) == "catalog.products.create"


# --- SellerGrant -----------------------------------------------------------
def _grant(status: SellerStatus, *permissions: Permission) -> SellerGrant:
    return SellerGrant(
        seller_id=SELLER_A, permissions=frozenset(permissions), status=status
    )


SELLER_A = uuid.uuid4()
SELLER_B = uuid.uuid4()
USER = uuid.uuid4()


def test_active_seller_grant_allows_a_held_permission():
    grant = _grant(SellerStatus.ACTIVE, Permission.SELLING_OFFERS_ACTIVATE)
    assert grant.allows(Permission.SELLING_OFFERS_ACTIVATE)


def test_grant_denies_a_permission_it_does_not_hold():
    grant = _grant(SellerStatus.ACTIVE, Permission.SELLING_OFFERS_ACTIVATE)
    assert not grant.allows(Permission.SELLING_OFFERS_PRICE_CHANGE)


@pytest.mark.parametrize("status", [SellerStatus.SUSPENDED, SellerStatus.PENDING])
def test_only_an_active_seller_confers_authority(status):
    """ADR-0011 §10.8 case I: a suspended seller must be denied even while the
    membership entry is still warm."""
    grant = _grant(status, Permission.SELLING_OFFERS_ACTIVATE)
    assert not grant.allows(Permission.SELLING_OFFERS_ACTIVATE)
    assert not status.grants_authority


def test_active_is_the_only_status_that_grants_authority():
    granting = [s for s in SellerStatus if s.grants_authority]
    assert granting == [SellerStatus.ACTIVE]


# --- Principal -------------------------------------------------------------
def test_principal_defaults_to_no_authority():
    """Deny by default: a bare Principal can do nothing."""
    principal = Principal(subject_id=USER)
    assert not principal.can(Permission.CATALOG_PRODUCTS_CREATE)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, SELLER_A)
    assert principal.seller_ids == frozenset()


def test_can_answers_platform_permissions():
    principal = Principal(
        subject_id=USER,
        platform_permissions=frozenset({Permission.CATALOG_PRODUCTS_CREATE}),
    )
    assert principal.can(Permission.CATALOG_PRODUCTS_CREATE)
    assert not principal.can(Permission.CATALOG_PRODUCTS_ARCHIVE)


def test_can_for_seller_is_scoped_to_the_membership():
    principal = Principal(
        subject_id=USER,
        memberships={
            SELLER_A: _grant(SellerStatus.ACTIVE, Permission.SELLING_OFFERS_ACTIVATE)
        },
    )
    assert principal.can_for_seller(Permission.SELLING_OFFERS_ACTIVATE, SELLER_A)
    # Same permission, a seller the principal has no membership in.
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_ACTIVATE, SELLER_B)


def test_a_platform_grant_does_not_satisfy_a_seller_scoped_question():
    """The regression that matters: holding a permission platform-wide must not
    silently confer it inside every seller. Overrides are explicit at the policy
    site (ADR-0011 §9)."""
    principal = Principal(
        subject_id=USER,
        platform_permissions=frozenset({Permission.SELLING_OFFERS_ACTIVATE}),
    )
    assert principal.can(Permission.SELLING_OFFERS_ACTIVATE)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_ACTIVATE, SELLER_A)


def test_membership_in_a_suspended_seller_confers_nothing():
    principal = Principal(
        subject_id=USER,
        memberships={
            SELLER_A: _grant(SellerStatus.SUSPENDED, Permission.SELLING_OFFERS_ACTIVATE)
        },
    )
    assert principal.is_member_of(SELLER_A)  # membership still exists
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_ACTIVATE, SELLER_A)


def test_seller_ids_lists_every_membership():
    principal = Principal(
        subject_id=USER,
        memberships={
            SELLER_A: _grant(SellerStatus.ACTIVE),
            SELLER_B: _grant(SellerStatus.SUSPENDED),
        },
    )
    assert principal.seller_ids == frozenset({SELLER_A, SELLER_B})


def test_principal_and_grant_are_immutable():
    """Assembly builds a Principal once; nothing downstream may widen it."""
    principal = Principal(subject_id=USER)
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.subject_id = uuid.uuid4()  # type: ignore[misc]
    grant = _grant(SellerStatus.ACTIVE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.status = SellerStatus.SUSPENDED  # type: ignore[misc]


def test_principal_carries_no_pii_fields():
    """ADR-0011 §4: a Principal is a capability set, not a user profile."""
    names = {f.name for f in dataclasses.fields(Principal)}
    assert names == {
        "subject_id", "tenant_id", "platform_permissions", "memberships",
    }


# --- ports -----------------------------------------------------------------
def test_seller_authorization_state_carries_only_security_fields():
    """ADR-0011 §10.2: four fields, no PII, no commercial attributes."""
    names = {f.name for f in dataclasses.fields(SellerAuthorizationState)}
    assert names == {"seller_id", "status", "tenant_id", "version"}


def test_ports_are_structural_protocols():
    """Adapters satisfy the ports by shape, so infrastructure never has to import
    the Shared Kernel to be substitutable."""

    class FakeRoleGrants:
        async def permission_codes_for_roles(self, role_codes):
            return frozenset({"catalog.products.create"})

    class FakeMemberships:
        async def memberships_for(self, user_id):
            return {}

        async def seller_states(self, seller_ids):
            return {}

    assert isinstance(FakeRoleGrants(), RoleGrantsQuery)
    assert isinstance(FakeMemberships(), MembershipQuery)


def test_assembly_drops_unknown_codes_and_keeps_known_ones():
    """The parse step every Principal assembly performs at the DB boundary."""
    from_db = ["catalog.products.create", "totally.made.up", "ops.dlq.replay"]
    parsed = frozenset(p for p in (from_code(c) for c in from_db) if p is not None)
    assert parsed == {Permission.CATALOG_PRODUCTS_CREATE, Permission.OPS_DLQ_REPLAY}
