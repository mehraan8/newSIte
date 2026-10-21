"""The platform RBAC baseline and drift computation (Phase 1E). DB-free.

The baseline is the seed contract: which platform roles exist, which permissions
exist, and which role holds which. `compute_drift` is pure, so it is tested here
as a truth table rather than against a database.
"""

from __future__ import annotations

import pytest

from src.modules.identity.application.rbac_baseline import (
    ROLE_PERMISSIONS,
    ActualRbacState,
    BaselineError,
    compute_drift,
    expected_state,
    validate_baseline,
)
from src.modules.identity.application.roles import PlatformRole
from src.modules.merchant.domain.entities import SellerMemberRole
from src.shared_kernel.application.permissions import Permission, PermissionScope


# --- baseline validity ------------------------------------------------------
def test_baseline_is_valid():
    validate_baseline()


def test_every_platform_role_has_a_baseline():
    assert set(ROLE_PERMISSIONS) == set(PlatformRole)


def test_no_platform_role_holds_a_seller_scopable_permission():
    """The invariant this phase turns on.

    A seller-scopable permission granted to a platform role would land in
    `Principal.platform_permissions`, where `can_for_seller` — strictly
    membership-scoped since Phase 1A — can never satisfy it. Cross-seller
    authority is expressed by the `.any` overrides, which are platform-scoped.
    """
    for role, permissions in ROLE_PERMISSIONS.items():
        offenders = sorted(p.code for p in permissions if p.is_seller_scopable)
        assert offenders == [], f"{role.code} holds seller-scopable {offenders}"


def test_validate_baseline_actually_rejects_a_seller_scopable_grant():
    """A guard that cannot fail proves nothing."""
    import src.modules.identity.application.rbac_baseline as mod

    original = mod.ROLE_PERMISSIONS
    mod.ROLE_PERMISSIONS = {
        **original,
        PlatformRole.CUSTOMER: frozenset({Permission.SELLING_OFFERS_CREATE}),
    }
    try:
        with pytest.raises(BaselineError, match="seller-scopable"):
            validate_baseline()
    finally:
        mod.ROLE_PERMISSIONS = original
    validate_baseline()  # restored


def test_validate_baseline_rejects_a_role_with_no_mapping():
    import src.modules.identity.application.rbac_baseline as mod

    original = mod.ROLE_PERMISSIONS
    mod.ROLE_PERMISSIONS = {
        r: p for r, p in original.items() if r is not PlatformRole.SUPPORT_AGENT
    }
    try:
        with pytest.raises(BaselineError, match="no baseline"):
            validate_baseline()
    finally:
        mod.ROLE_PERMISSIONS = original


# --- the mapping is explainable ---------------------------------------------
def test_catalog_admin_holds_no_pricing_or_stock_authority():
    """Its purpose statement says so explicitly."""
    codes = {p.code for p in ROLE_PERMISSIONS[PlatformRole.CATALOG_ADMIN]}
    assert all(c.startswith("catalog.") for c in codes)
    assert not any(c.startswith(("selling.", "inventory.")) for c in codes)


def test_inventory_manager_cannot_create_products_or_set_prices():
    codes = {p.code for p in ROLE_PERMISSIONS[PlatformRole.INVENTORY_MANAGER]}
    assert not any(c.startswith("catalog.") for c in codes)
    assert not any("price" in c for c in codes)
    # Cross-seller stock authority comes from the platform override.
    assert Permission.INVENTORY_MANAGE_ANY in ROLE_PERMISSIONS[PlatformRole.INVENTORY_MANAGER]


def test_support_agent_reads_and_cancels_orders_and_mutates_nothing_else():
    perms = ROLE_PERMISSIONS[PlatformRole.SUPPORT_AGENT]
    assert perms == {
        Permission.ORDERING_ORDERS_READ_ANY, Permission.ORDERING_ORDERS_CANCEL_ANY
    }


def test_readonly_operator_holds_zero_write_capability():
    """dlq.replay re-emits real events and reconciliation.run mutates stock, so
    neither belongs to a read-only role despite sitting among the ops codes."""
    perms = ROLE_PERMISSIONS[PlatformRole.READONLY_OPERATOR]
    assert Permission.OPS_DLQ_REPLAY not in perms
    assert Permission.OPS_RECONCILIATION_RUN not in perms
    for p in perms:
        assert ".read" in p.code or p.code.endswith(".read"), p.code


def test_customer_can_only_reach_its_own_orders():
    perms = ROLE_PERMISSIONS[PlatformRole.CUSTOMER]
    assert Permission.ORDERING_ORDERS_READ_ANY not in perms
    assert Permission.ORDERING_ORDERS_CANCEL_ANY not in perms
    assert Permission.ORDERING_ORDERS_READ_OWN in perms


def test_super_admin_holds_every_platform_permission():
    expected = {p for p in Permission if p.scope is PermissionScope.PLATFORM}
    assert ROLE_PERMISSIONS[PlatformRole.SUPER_ADMIN] == expected
    assert Permission.IDENTITY_ROLES_GRANT in expected


def test_role_granting_is_super_admin_only():
    for role, perms in ROLE_PERMISSIONS.items():
        if role is PlatformRole.SUPER_ADMIN:
            continue
        assert Permission.IDENTITY_ROLES_GRANT not in perms
        assert Permission.IDENTITY_ROLES_REVOKE not in perms


def test_no_platform_role_is_a_seller_role():
    assert "seller" not in {r.code for r in PlatformRole}
    assert {r.code for r in PlatformRole}.isdisjoint({r.value for r in SellerMemberRole})


# --- determinism ------------------------------------------------------------
def test_expected_state_is_fully_sorted():
    state = expected_state()
    assert list(state.roles) == sorted(state.roles)
    assert list(state.permissions) == sorted(state.permissions)
    assert list(state.grants) == sorted(state.grants)


def test_expected_state_is_deterministic_across_repeated_calls():
    fingerprints = {expected_state().fingerprint() for _ in range(10)}
    assert len(fingerprints) == 1


def test_expected_state_covers_the_whole_vocabulary():
    state = expected_state()
    assert {c for c, _ in state.permissions} == {p.code for p in Permission}
    assert {c for c, _, _ in state.roles} == {r.code for r in PlatformRole}
    assert all(is_system for _, _, is_system in state.roles)


def test_fingerprint_changes_when_the_baseline_changes():
    import src.modules.identity.application.rbac_baseline as mod

    before = expected_state().fingerprint()
    original = mod.ROLE_PERMISSIONS
    mod.ROLE_PERMISSIONS = {
        **original,
        PlatformRole.CUSTOMER: original[PlatformRole.CUSTOMER] | {Permission.OPS_OUTBOX_READ},
    }
    try:
        assert expected_state().fingerprint() != before
    finally:
        mod.ROLE_PERMISSIONS = original
    assert expected_state().fingerprint() == before


# --- drift computation ------------------------------------------------------
def actual_matching_baseline() -> ActualRbacState:
    state = expected_state()
    return ActualRbacState(
        role_codes=frozenset(c for c, _, _ in state.roles),
        permission_codes=frozenset(c for c, _ in state.permissions),
        grants=frozenset(state.grants),
    )


def test_clean_when_database_matches_the_baseline():
    drift = compute_drift(expected_state(), actual_matching_baseline())
    assert drift.is_clean
    assert drift.render() == "RBAC DRIFT: CLEAN"


def test_empty_database_reports_everything_missing():
    drift = compute_drift(expected_state(), ActualRbacState())
    assert not drift.is_clean
    assert len(drift.missing_permissions) == len(list(Permission))
    assert len(drift.missing_roles) == len(list(PlatformRole))
    assert len(drift.missing_grants) == len(expected_state().grants)
    assert drift.unknown_permissions == () and drift.unknown_roles == ()


def test_unknown_permission_in_the_database_is_reported():
    actual = actual_matching_baseline()
    drift = compute_drift(
        expected_state(),
        ActualRbacState(
            role_codes=actual.role_codes,
            permission_codes=actual.permission_codes | {"quantum.teleport.everything"},
            grants=actual.grants,
        ),
    )
    assert drift.unknown_permissions == ("quantum.teleport.everything",)
    assert drift.missing_permissions == ()
    assert not drift.is_clean


def test_unknown_role_in_the_database_is_reported():
    actual = actual_matching_baseline()
    drift = compute_drift(
        expected_state(),
        ActualRbacState(
            role_codes=actual.role_codes | {"seller", "wizard"},
            permission_codes=actual.permission_codes,
            grants=actual.grants,
        ),
    )
    assert drift.unknown_roles == ("seller", "wizard")


def test_missing_and_unexpected_grants_are_reported_separately():
    state = expected_state()
    actual = actual_matching_baseline()
    dropped = state.grants[0]
    drift = compute_drift(
        state,
        ActualRbacState(
            role_codes=actual.role_codes,
            permission_codes=actual.permission_codes,
            grants=(actual.grants - {dropped}) | {("customer", "ops.dlq.replay")},
        ),
    )
    assert drift.missing_grants == (dropped,)
    assert drift.unexpected_grants == (("customer", "ops.dlq.replay"),)


def test_drift_report_is_deterministic_and_readable():
    actual = actual_matching_baseline()
    dirty = ActualRbacState(
        role_codes=actual.role_codes | {"wizard"},
        permission_codes=actual.permission_codes - {Permission.OPS_DLQ_READ.code},
        grants=actual.grants | {("customer", "ops.dlq.replay")},
    )
    renders = {compute_drift(expected_state(), dirty).render() for _ in range(5)}
    assert len(renders) == 1
    report = renders.pop()
    assert report.startswith("RBAC DRIFT: DETECTED")
    assert "Missing permissions:" in report
    assert "Unknown roles:" in report
    assert "Unexpected grants:" in report
    assert "customer -> ops.dlq.replay" in report


def test_drift_never_invents_authority():
    """An unknown DB permission must never become a Permission member — the
    fail-closed contract from Phase 1B, restated at the drift boundary."""
    from src.shared_kernel.application.permissions import from_code

    drift = compute_drift(
        expected_state(),
        ActualRbacState(permission_codes=frozenset({"quantum.teleport.everything"})),
    )
    for code in drift.unknown_permissions:
        assert from_code(code) is None
