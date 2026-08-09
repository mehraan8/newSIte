"""The authorization matrix: every operation × every kind of caller.

This is the regression lock for CRITICAL 1 (ten unauthenticated write endpoints)
and CRITICAL 2 (role checks that did not exist). It asserts the GATE only — a
guarded route reached by an authorized caller may legitimately answer 404 or 422
for the throwaway ids these tests send. What must never happen is an anonymous
caller getting past the gate at all.
"""

from __future__ import annotations

import pytest

from src.api.main import app
from src.modules.identity.domain.roles import Role
from tests.api import routes
from tests.api.conftest import auth, call, principal

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


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) in (Role.SELLER, Role.ADMIN)],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_role_gated_operation_rejects_plain_user(client, method, path):
    """An authenticated customer is not an admin or a seller."""
    _uid, headers = principal()  # no roles granted
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
    [op for op in _operations() if routes.ACCESS.get(op) == Role.ADMIN],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_admin_route_rejects_seller(client, method, path):
    """Roles are not hierarchical: a seller must not reach back-office routes."""
    _uid, headers = principal(Role.SELLER)
    resp = call(
        client, method, routes.concrete(path),
        json=routes.body_for(method, path), headers=headers,
    )
    assert resp.status_code == 403, (
        f"{method} {path} let a SELLER through with {resp.status_code}; expected 403."
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [op for op in _operations() if routes.ACCESS.get(op) == Role.SELLER],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_seller_route_rejects_admin(client, method, path):
    """Being an admin does not make you a seller — no implicit role widening."""
    _uid, headers = principal(Role.ADMIN)
    resp = call(
        client, method, routes.concrete(path),
        json=routes.body_for(method, path), headers=headers,
    )
    assert resp.status_code == 403


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
    required = routes.ACCESS[(method, path)]
    roles = () if required == routes.AUTHENTICATED else (required,)
    _uid, headers = principal(*roles)
    resp = call(
        client, method, routes.concrete(path),
        json=routes.body_for(method, path), headers=headers,
    )
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
    """Signature validity is not authorization. Regression for HIGH 4."""
    uid, headers = principal(status="blocked")
    resp = call(client, "GET", "/api/v1/auth/me", headers=headers)
    assert resp.status_code == 403, (
        f"a blocked user reached /me with {resp.status_code}: {resp.text[:200]}"
    )


def test_role_revocation_takes_effect_without_a_new_token(client):
    """Roles are re-read per request, so a revoked grant stops working at once
    rather than lasting until the access token expires."""
    from tests.integration import db

    uid, headers = principal(Role.ADMIN)
    first = call(
        client, "POST", "/api/v1/inventory/stock",
        json=routes.body_for("POST", "/api/v1/inventory/stock"), headers=headers,
    )
    assert first.status_code != 403

    db.execute("delete from user_roles where user_id = :u", u=uid)

    after = call(
        client, "POST", "/api/v1/inventory/stock",
        json=routes.body_for("POST", "/api/v1/inventory/stock"), headers=headers,
    )
    assert after.status_code == 403, (
        "a revoked role still worked with the old token — roles must not be "
        "trusted from the token body"
    )
