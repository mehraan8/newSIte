"""The authoritative platform RBAC baseline, and drift detection over it.

ADR-0011 §5 makes the `Permission` enum the source of truth for what permissions
EXIST, and Phase 1D made `PlatformRole` the source of truth for which platform
roles exist. Neither says which role holds which permission — the ADR only states
that `role_permissions` is database-backed. This module supplies the missing
piece: the **seed baseline** for the system roles, expressed in code so that it
is reviewable, deterministic, and drift-detectable.

    Permission enum + PlatformRole + ROLE_PERMISSIONS   (code, authoritative)
                        │
                        ├── expected_state()  -> deterministic, sorted
                        │        │
                        │        ├── generated seed migration literals
                        │        └── compared against the live database
                        ▼
                   compute_drift()

Reconciliation with ADR-0011 §5. The ADR calls `role_permissions` "DB-owned",
which is about STORAGE, not about who decides the defaults: an admin surface may
later add grants at runtime. This module is therefore the authoritative
**baseline for system roles**, and `compute_drift` reports anything beyond it as
`unexpected_grants` — informative, not automatically wrong.

THE INVARIANT THAT MATTERS MOST. A platform role may hold only PLATFORM-scoped
permissions. A seller-scopable permission granted to a platform role would land
in `Principal.platform_permissions`, where `can_for_seller` — which is strictly
membership-scoped (Phase 1A) — can never satisfy it. Such a grant would be dead
weight at best, and at worst would read as though a platform role conferred
seller authority. Cross-seller authority is expressed by the `.any` overrides,
which are platform-scoped by construction. `validate_baseline()` enforces this.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.modules.identity.application.roles import PlatformRole
from src.shared_kernel.application.permissions import Permission, PermissionScope

P = Permission
R = PlatformRole

# --- the baseline ----------------------------------------------------------
# Every grant below is derived from the role's own purpose statement in
# `roles.py`; the comment on each entry quotes the clause it comes from.
ROLE_PERMISSIONS: Mapping[PlatformRole, frozenset[Permission]] = {
    # "Buys; sees only their own orders." Catalog and buy-box reads are the
    # anonymous surface, and /auth/me is merely-authenticated, so neither needs
    # a grant — a permission is only granted where a check will consult it.
    R.CUSTOMER: frozenset({
        P.ORDERING_ORDERS_CREATE,
        P.ORDERING_ORDERS_READ_OWN,
        P.ORDERING_ORDERS_CANCEL_OWN,
    }),
    # "Owns the product graph. Deliberately holds no pricing or stock authority."
    # Hence nothing from selling.* or inventory.*.
    R.CATALOG_ADMIN: frozenset({
        P.CATALOG_PRODUCTS_CREATE,
        P.CATALOG_PRODUCTS_UPDATE,
        P.CATALOG_PRODUCTS_PUBLISH,
        P.CATALOG_PRODUCTS_ARCHIVE,
        P.CATALOG_TAXONOMY_MANAGE,
    }),
    # "First-party stock operations. Cannot create products or set prices."
    # inventory.stock.receive is seller-scopable, so cross-seller stock authority
    # comes from the inventory.manage.any override instead.
    R.INVENTORY_MANAGER: frozenset({
        P.INVENTORY_STOCK_READ_ANY,
        P.INVENTORY_RESERVATIONS_CREATE,
        P.INVENTORY_RESERVATIONS_COMMIT,
        P.INVENTORY_RESERVATIONS_RELEASE,
        P.INVENTORY_MANAGE_ANY,
    }),
    # "Reads any order and cancels on a customer's behalf. Mutates nothing else."
    R.SUPPORT_AGENT: frozenset({
        P.ORDERING_ORDERS_READ_ANY,
        P.ORDERING_ORDERS_CANCEL_ANY,
    }),
    # "Observability and incident response with zero write capability."
    # Deliberately excludes ops.dlq.replay and ops.reconciliation.run: both
    # re-emit or mutate real state despite sitting among the ops permissions.
    R.READONLY_OPERATOR: frozenset({
        P.INVENTORY_STOCK_READ_ANY,
        P.ORDERING_ORDERS_READ_ANY,
        P.SELLING_OFFERS_READ_ANY,
        P.MERCHANT_SELLERS_READ_ANY,
        P.NOTIFICATION_MESSAGES_READ_ANY,
        P.OPS_OUTBOX_READ,
        P.OPS_DLQ_READ,
        P.OPS_AUDIT_READ,
    }),
    # "Break-glass. Holds every permission, including role granting." Computed
    # rather than listed, so a new platform permission reaches super_admin
    # automatically and cannot be forgotten. Seller-scopable permissions are
    # excluded for the reason in the module docstring — super_admin acts across
    # sellers through the .any overrides, all of which are platform-scoped.
    R.SUPER_ADMIN: frozenset(
        p for p in Permission if p.scope is PermissionScope.PLATFORM
    ),
}


class BaselineError(Exception):
    """The baseline itself is malformed — a programming error, not drift."""


def validate_baseline() -> None:
    """Fail loudly on a baseline that violates the architecture.

    Called by the tests and by the drift entrypoint, so a bad grant cannot reach
    a seed migration.
    """
    missing = set(PlatformRole) - set(ROLE_PERMISSIONS)
    if missing:
        raise BaselineError(f"roles with no baseline: {sorted(r.code for r in missing)}")

    for role, permissions in ROLE_PERMISSIONS.items():
        scopable = sorted(p.code for p in permissions if p.is_seller_scopable)
        if scopable:
            raise BaselineError(
                f"platform role {role.code!r} is granted seller-scopable "
                f"permission(s) {scopable}; seller authority comes from a "
                f"SellerMember grant, never from a platform role"
            )


# --- expected state (deterministic) ----------------------------------------
@dataclass(frozen=True)
class RbacBaseline:
    """The seed state, fully ordered so output is byte-stable across runs."""

    roles: tuple[tuple[str, str, bool], ...]          # (code, name_fa, is_system)
    permissions: tuple[tuple[str, str], ...]          # (code, description)
    grants: tuple[tuple[str, str], ...]               # (role_code, permission_code)

    def fingerprint(self) -> str:
        """Stable hash of the whole baseline.

        The CI gate compares this against the value frozen into the seed
        migration, so changing the enum, the role list or the mapping without
        regenerating the migration fails the build.
        """
        canonical = "\n".join(
            [f"role\t{c}\t{n}\t{int(s)}" for c, n, s in self.roles]
            + [f"perm\t{c}\t{d}" for c, d in self.permissions]
            + [f"grant\t{r}\t{p}" for r, p in self.grants]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def expected_state() -> RbacBaseline:
    """The baseline as the database should hold it. Sorted; no timestamps, no
    UUIDs, no surrogate ids — nothing that could vary between runs."""
    validate_baseline()
    return RbacBaseline(
        roles=tuple(sorted((r.code, r.name_fa, True) for r in PlatformRole)),
        permissions=tuple(sorted((p.code, p.description) for p in Permission)),
        grants=tuple(sorted(
            (role.code, permission.code)
            for role, permissions in ROLE_PERMISSIONS.items()
            for permission in permissions
        )),
    )


# --- drift -----------------------------------------------------------------
@dataclass(frozen=True)
class ActualRbacState:
    """What the database currently holds. Codes only — surrogate ids are a
    relational detail and never part of the authorization vocabulary."""

    role_codes: frozenset[str] = frozenset()
    permission_codes: frozenset[str] = frozenset()
    grants: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class RbacDrift:
    missing_permissions: tuple[str, ...] = ()     # in code, absent from DB
    unknown_permissions: tuple[str, ...] = ()     # in DB, absent from code
    missing_roles: tuple[str, ...] = ()
    unknown_roles: tuple[str, ...] = ()
    missing_grants: tuple[tuple[str, str], ...] = ()
    unexpected_grants: tuple[tuple[str, str], ...] = ()

    @property
    def is_clean(self) -> bool:
        return not any((
            self.missing_permissions, self.unknown_permissions,
            self.missing_roles, self.unknown_roles,
            self.missing_grants, self.unexpected_grants,
        ))

    def render(self) -> str:
        """Human-readable report; deterministic because every list is sorted."""
        if self.is_clean:
            return "RBAC DRIFT: CLEAN"
        lines = ["RBAC DRIFT: DETECTED", ""]
        for title, items in (
            ("Missing permissions", self.missing_permissions),
            ("Unknown permissions", self.unknown_permissions),
            ("Missing roles", self.missing_roles),
            ("Unknown roles", self.unknown_roles),
        ):
            if items:
                lines.append(f"{title}:")
                lines += [f"  {i}" for i in items]
                lines.append("")
        for title, pairs in (
            ("Missing grants", self.missing_grants),
            ("Unexpected grants", self.unexpected_grants),
        ):
            if pairs:
                lines.append(f"{title}:")
                lines += [f"  {r} -> {p}" for r, p in pairs]
                lines.append("")
        return "\n".join(lines).rstrip()


def compute_drift(expected: RbacBaseline, actual: ActualRbacState) -> RbacDrift:
    """Pure comparison — no I/O, so it is exhaustively testable as a truth table.

    `unknown_permissions` is reported but is NOT an authorization hazard on its
    own: Principal assembly already drops codes the enum does not define
    (ADR-0011 §5, Phase 1B). It is reported so a database ahead of its deployed
    code is visible rather than silent.
    """
    expected_permissions = {code for code, _ in expected.permissions}
    expected_roles = {code for code, _, _ in expected.roles}
    expected_grants = set(expected.grants)

    return RbacDrift(
        missing_permissions=tuple(sorted(expected_permissions - actual.permission_codes)),
        unknown_permissions=tuple(sorted(actual.permission_codes - expected_permissions)),
        missing_roles=tuple(sorted(expected_roles - actual.role_codes)),
        unknown_roles=tuple(sorted(actual.role_codes - expected_roles)),
        missing_grants=tuple(sorted(expected_grants - actual.grants)),
        unexpected_grants=tuple(sorted(actual.grants - expected_grants)),
    )
