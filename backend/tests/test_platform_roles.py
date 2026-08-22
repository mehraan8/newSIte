"""Platform role vocabulary (ADR-0011 §6, Phase 1D). DB-free.

Platform roles and seller membership are two separate authorization systems.
These tests exist mostly to keep them separate.
"""

from __future__ import annotations

import pathlib
import re

from src.modules.identity.application.roles import (
    PlatformRole,
    PlatformRoleSpec,
    all_codes,
    from_code,
)
from src.modules.merchant.domain.entities import SellerMemberRole

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def test_vocabulary_is_exactly_the_adr_role_set():
    assert all_codes() == {
        "customer", "catalog_admin", "inventory_manager",
        "support_agent", "readonly_operator", "super_admin",
    }


def test_there_is_no_seller_platform_role():
    """The rule this whole phase is built around: selling capability is a
    membership, never a global grant (ADR-0011 §6)."""
    assert "seller" not in all_codes()
    for code in all_codes():
        assert "seller" not in code


def test_platform_roles_and_member_roles_share_no_codes():
    """A member role code must never be assignable as a platform role, or the
    two systems would silently merge."""
    member_codes = {r.value for r in SellerMemberRole}
    assert all_codes().isdisjoint(member_codes)
    assert member_codes == {
        "seller_owner", "seller_manager", "seller_staff", "seller_viewer"
    }


def test_anonymous_is_not_a_grantable_role():
    """It is the absence of a principal, not a grant — there is no row to assign."""
    assert "anonymous" not in all_codes()
    assert from_code("anonymous") is None


def test_every_role_carries_the_metadata_the_seeder_needs():
    for role in PlatformRole:
        assert isinstance(role.value, PlatformRoleSpec)
        assert role.code and role.code == role.code.lower()
        assert role.name_fa.strip(), f"{role.name} has no Persian name"
        assert role.description.strip()


def test_codes_are_unique_and_round_trip():
    codes = [r.code for r in PlatformRole]
    assert len(codes) == len(set(codes))
    for role in PlatformRole:
        assert from_code(role.code) is role


def test_unknown_role_code_fails_closed():
    """A database holding a role this build never heard of confers nothing."""
    for unknown in ("seller", "wizard", "", "SUPER_ADMIN", "admin"):
        assert from_code(unknown) is None


def test_str_is_the_code():
    assert str(PlatformRole.SUPER_ADMIN) == "super_admin"


def test_role_vocabulary_lives_outside_the_domain_layer():
    """ADR-0011 principle 1 keeps Role and Permission out of the domain."""
    assert not (SRC / "modules" / "identity" / "domain" / "roles.py").exists()
    offenders = [
        p.relative_to(SRC) for p in SRC.rglob("domain/*.py")
        if re.search(r"^\s*(from|import).*application\.roles", p.read_text(encoding="utf-8"), re.M)
    ]
    assert offenders == [], f"domain imports the role vocabulary: {offenders}"


def test_platform_role_resolution_sql_never_touches_merchant_tables():
    """The actual SQL executed by the adapters, not prose about it.

    Checked against the query constants themselves rather than the file text,
    because both modules legitimately DISCUSS seller membership in order to
    explain what they must not do.
    """
    from src.modules.identity.infrastructure import role_repository as rr

    for name in ("_ROLE_CODES_FOR_USER", "_PERMISSION_CODES_FOR_ROLES"):
        sql = str(getattr(rr, name)).lower()
        for forbidden in ("seller", "merchant"):
            assert forbidden not in sql, f"{name} queries {forbidden}"
        # It must resolve through stable CODES, never surrogate ids (ADR-0011 §12).
        assert "code" in sql


def test_no_identity_module_imports_merchant():
    """Platform RBAC must not depend on Merchant at all (RULE 6)."""
    import ast

    identity = SRC / "modules" / "identity"
    offenders = []
    for path in identity.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.startswith("src.modules.merchant") for n in names):
                offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"Identity imports Merchant: {offenders}"
