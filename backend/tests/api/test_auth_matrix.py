"""The authorization matrix: every operation × every kind of caller.

This is the regression lock for CRITICAL 1 (ten unauthenticated write endpoints)
and CRITICAL 2 (authorization checks that did not exist). It asserts the GATE
only — a guarded route reached by an authorized caller may legitimately answer
404 or 422 for the throwaway ids these tests send. What must never happen is an
anonymous caller getting past the gate at all.

The two-way coverage check is the durable part: adding a route forces an explicit
decision about who may call it, which is exactly what was missing when ten
endpoints shipped open.

SCOPE NOTE. The old platform-role assertions are gone because the role system
they tested was replaced in Phases 1A–1E. Seller operations are asserted as a
membership, not a role. Back-office operations, which the security audit found
reachable by any logged-in customer, are now asserted in full: refused for a
role-less user, admitted for the role that holds the permission.
"""

from __future__ import annotations

import pytest

from src.api.main import app
from src.modules.identity.application.roles import PlatformRole
from tests.api import routes
from tests.api.conftest import (
    call,
    make_seller,
    member,
    principal,
    seed_offer,
    seed_variant,
)

pytestmark = pytest.mark.integration

GATE_STATUSES = {401, 403}


def _operations() -> list[tuple[str, str]]:
    """Live operations, from the app's own schema rather than a hand-kept list."""
    spec = app.openapi()
    ops = []
    for path, methods in spec["paths"].items():
        if path in routes.IGNORED_PATHS:
            continue
        for method in methods:
            ops.append((method.upper(), path))
    return sorted(ops)


def test_every_operation_declares_an_access_level():
    """A new endpoint must state who may call it, or this suite fails.

    Without this, adding a route silently inherits whatever the router happens to
    do — which is precisely how the original ten endpoints shipped open.
    """
    undeclared = [op for op in _operations() if op not in routes.ACCESS]
    assert not undeclared, (
        "These operations have no entry in tests/api/routes.ACCESS. Add one and "
        f"make an explicit decision about who may call them: {undeclared}"
    )


def test_access_table_has_no_stale_entries():
    live = set(_operations())
    stale = [op for op in routes.ACCESS if op not in live]
    assert not stale, f"ACCESS lists operations that no longer exist: {stale}"


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) != routes.PUBLIC],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_protected_operation_rejects_anonymous(client, method, path):
    resp = call(client, method, routes.concrete(path), json=routes.body_for(method, path))
    assert resp.status_code == 401, (
        f"{method} {path} answered {resp.status_code} to an ANONYMOUS caller; "
        "expected 401. A write endpoint reachable without credentials is "
        f"CRITICAL. Body: {resp.text[:200]}"
    )


def seller_scoped_request(method: str, path: str, seller_id=None) -> tuple[str, dict | None]:
    """(url, body) for a seller-scoped operation, with a REAL offer behind it.

    These routes resolve the offer before authorizing, so a throwaway id answers
    404 — correct behaviour, and useless as an authorization assertion. Seeding a
    real offer owned by `seller_id` (by default a seller the caller has nothing
    to do with) is what makes 403 the only acceptable answer.

    The offer id travels in the path for the Selling routes and in the BODY for
    `POST /inventory/stock`, so both are substituted.
    """
    body = routes.body_for(method, path)
    if body is not None and "seller_id" in body and seller_id is not None:
        body = {**body, "seller_id": str(seller_id)}

    needs_offer = "{offer_id}" in path or (body is not None and "offer_id" in body)
    if not needs_offer:
        return routes.concrete(path), body

    owner = seller_id if seller_id is not None else make_seller()
    offer = seed_offer(owner, seed_variant(), status="active", amount=50_000)
    if body is not None and "offer_id" in body:
        body = {**body, "offer_id": str(offer)}
    return routes.concrete(path.replace("{offer_id}", str(offer))), body


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) == routes.SELLER_MEMBER],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_seller_operation_rejects_a_caller_with_no_membership(client, method, path):
    """Being logged in is not being a seller. No role changes that — only a
    SellerMember row does."""
    _uid, headers = principal()  # authenticated, no platform role, no membership
    url, body = seller_scoped_request(method, path)
    resp = call(client, method, url, json=body, headers=headers)
    assert resp.status_code == 403, (
        f"{method} {path} answered {resp.status_code} to a caller with no seller "
        f"membership; expected 403. Body: {resp.text[:200]}"
    )


# `POST /inventory/stock` deliberately honours a platform override
# (`inventory.manage.any`) so back office can correct stock across sellers —
# see inventory/application/policies.py. It is therefore excluded from the
# no-override rule below and asserted separately, rather than the rule being
# quietly softened for everyone.
PLATFORM_OVERRIDE_ALLOWED = {("POST", "/api/v1/inventory/stock")}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        op for op in _operations()
        if routes.ACCESS.get(op) == routes.SELLER_MEMBER
        and op not in PLATFORM_OVERRIDE_ALLOWED
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_seller_operation_rejects_a_platform_super_admin(client, method, path):
    """No cross-universe widening (approval decisions E/F).

    super_admin holds every PLATFORM permission, including
    `selling.offers.manage.any`. It still is not a member of any seller, and
    acting AS a seller is what these routes do. Selling grants NO platform
    override at all; Inventory grants exactly one, listed above.
    """
    _uid, headers = principal(PlatformRole.SUPER_ADMIN.code)
    url, body = seller_scoped_request(method, path)
    resp = call(client, method, url, json=body, headers=headers)
    assert resp.status_code == 403, (
        f"{method} {path} let a platform super_admin act as a seller with "
        f"{resp.status_code}; platform authority is not seller authority."
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) == routes.SELLER_MEMBER],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_seller_member_passes_the_gate(client, method, path):
    """The counterweight: a real owner member must get past authorization.

    Beyond the gate a 404/409/422 is fine — these ids are throwaway. Only 401/403
    would mean the guard is too broad.
    """
    seller, _uid, headers = member()
    url, body = seller_scoped_request(method, path, seller_id=seller)
    resp = call(client, method, url, json=body, headers=headers)
    assert resp.status_code not in GATE_STATUSES, (
        f"{method} {path} refused a seller_owner member with {resp.status_code}: "
        f"{resp.text[:200]}"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) != routes.PUBLIC],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_authorized_caller_passes_the_gate(client, method, path):
    """The guard must not be so broad that it blocks legitimate callers.

    A test suite that only proves things are denied would pass with every
    endpoint hard-wired to 403.
    """
    body = routes.body_for(method, path)
    if routes.ACCESS[(method, path)] == routes.PLATFORM_GUARDED:
        # "Correctly authorized" here means holding the platform role that
        # carries the route's permission — the counterweight proving the new
        # guards did not simply wall the endpoints off from everyone.
        _uid, headers = principal(routes.PLATFORM_ROLE_REQUIRED[(method, path)])
        url = routes.concrete(path)
    elif routes.ACCESS[(method, path)] == routes.SELLER_MEMBER:
        # "Correctly authorized" for a seller-scoped route means a member of the
        # seller that owns the resource — there is no role that grants it.
        seller, _uid, headers = member()
        url, body = seller_scoped_request(method, path, seller_id=seller)
    else:
        _uid, headers = principal()
        url = routes.concrete(path)
    resp = call(client, method, url, json=body, headers=headers)
    assert resp.status_code not in GATE_STATUSES, (
        f"{method} {path} refused a correctly-authorized caller with "
        f"{resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) == routes.PUBLIC],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_public_operation_needs_no_credentials(client, method, path):
    resp = call(client, method, routes.concrete(path), json=routes.body_for(method, path))
    assert resp.status_code not in GATE_STATUSES or path.startswith("/api/v1/auth/"), (
        f"{method} {path} is declared PUBLIC but answered {resp.status_code}"
    )


def test_blocked_user_cannot_use_a_valid_token(client):
    """Signature validity is not authorization. Regression for HIGH 4.

    STATUS CODE CHANGED IN PHASE 1G.1: 403 -> 401. This test previously encoded
    the pre-ADR-0011 contract, where a blocked account surfaced as a
    `ForbiddenError` from the login use case. The account gate is now part of
    AUTHENTICATION (`get_access_claims`), and 401 is what that means: the caller
    has not established who they are to the system's satisfaction. 403 would
    assert the opposite — "you are authenticated, you merely lack a permission" —
    which is false for a blocked account, and would also make an account-level
    lockout indistinguishable from an ordinary authorization denial in logs and
    in client retry logic.

    Phase 1G.1 §4 states the required invariant verbatim as
    `valid token + blocked status = 401`.

    The behaviour under test is unchanged and stronger than before: it used to
    fail outright, because nothing read `users.status` at request time. The
    deeper coverage lives in tests/api/test_identity_session_security.py.
    """
    uid, headers = principal(status="blocked")
    resp = call(client, "GET", "/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401, (
        f"a blocked user reached /me with {resp.status_code}: {resp.text[:200]}"
    )


def test_permissions_are_resolved_per_request_and_never_carried_in_the_token():
    """The token names ROLES; the database decides what those roles may do.

    ADR-0011 §8: no permission ever enters a JWT. So changing a role's grants
    takes effect on the very next request with the same token — asserted here
    through a throwaway role, so no seeded row is disturbed.

    The previous version of this test asserted that revoking a *role* took effect
    immediately. It does not, by design: role codes are stamped into the access
    token and re-stamped on refresh. Asserting otherwise would have been
    asserting a property the system deliberately does not have.
    """
    import uuid as _uuid

    from src.api.security import PrincipalFactory
    from src.modules.identity.application.ports import AccessTokenClaims
    from src.modules.identity.infrastructure.role_repository import SqlRoleGrantsQuery
    from src.shared_kernel.application.permissions import Permission
    from tests.api.conftest import make_user
    from tests.integration import db

    code = f"qa_temp_{_uuid.uuid4().hex[:8]}"
    uid = make_user()
    granted = Permission.CATALOG_PRODUCTS_CREATE

    async def _assemble():
        async with db.session_factory()() as session:
            return await PrincipalFactory(SqlRoleGrantsQuery(session)).from_claims(
                AccessTokenClaims(uid, None, (code,))
            )

    role_id = None
    try:
        db.execute(
            "insert into roles (code, name_fa, is_system) values (:c, :n, false)",
            c=code, n="نقش موقت",
        )
        role_id = db.scalar("select id from roles where code = :c", c=code)
        perm_id = db.scalar("select id from permissions where code = :c", c=granted.code)
        assert perm_id is not None, "migration 0020 should have seeded permissions"
        db.execute(
            "insert into role_permissions (role_id, permission_id) values (:r, :p)",
            r=role_id, p=perm_id,
        )

        assert granted in db.run(_assemble()).platform_permissions

        db.execute("delete from role_permissions where role_id = :r", r=role_id)

        assert db.run(_assemble()).platform_permissions == frozenset(), (
            "the principal kept a permission after its grant was removed; "
            "permissions must be resolved per request, never read from the token"
        )
    finally:
        if role_id is not None:
            db.execute("delete from role_permissions where role_id = :r", r=role_id)
            db.execute("delete from user_roles where role_id = :r", r=role_id)
            db.execute("delete from roles where id = :r", r=role_id)


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) == routes.PLATFORM_GUARDED],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_back_office_operation_rejects_a_role_less_user(client, method, path):
    """The audit's CRITICAL finding, locked.

    Every one of these answered 2xx to an ordinary authenticated customer:
    creating and editing catalogue products, injecting stock against another
    seller's offer, and committing or releasing any reservation by id. Being
    logged in is not being an operator.
    """
    _uid, headers = principal()  # authenticated, no platform role
    resp = call(
        client, method, routes.concrete(path),
        json=routes.body_for(method, path), headers=headers,
    )
    assert resp.status_code == 403, (
        f"{method} {path} answered {resp.status_code} to a role-less authenticated "
        f"caller; expected 403. Body: {resp.text[:200]}"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) == routes.PLATFORM_GUARDED],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_back_office_operation_rejects_a_seller_member(client, method, path):
    """Seller authority is not platform authority (ADR-0011 §7).

    A seller_owner has full command of their own shop. That must not extend to
    the catalogue every seller shares, nor to another seller's reservations.
    """
    _seller, _uid, headers = member()
    resp = call(
        client, method, routes.concrete(path),
        json=routes.body_for(method, path), headers=headers,
    )
    assert resp.status_code == 403, (
        f"{method} {path} let a seller member into back office with "
        f"{resp.status_code}"
    )


def test_the_inventory_platform_override_actually_works(client):
    """The one deliberate override, asserted rather than assumed.

    `inventory.manage.any` exists so an operator can correct stock they do not
    own. If this stopped working, back office would be locked out and the
    exclusion above would be silently protecting nothing.
    """
    seller = make_seller()
    offer = seed_offer(seller, seed_variant(), status="active", amount=1000)
    _uid, headers = principal(PlatformRole.INVENTORY_MANAGER.code)

    resp = call(
        client, "POST", "/api/v1/inventory/stock", headers=headers,
        json={"offer_id": str(offer), "location": "default", "quantity": 3},
    )

    assert resp.status_code == 200, resp.text
