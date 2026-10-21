"""Access-token claims and Principal assembly (ADR-0011 §5, §8; Phase 1B).

DB-free: the JWT service is pure, and Principal assembly is exercised against
fake ports. What is asserted here is the token CONTRACT — which claims exist,
which must never exist, and what happens when a claim is malformed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import jwt as pyjwt
import pytest

from src.config.settings import settings
from src.api.security import PrincipalFactory
from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.identity.infrastructure.jwt_service import (
    ACCESS_TOKEN_CLAIMS,
    JwtTokenService,
)
from src.shared_kernel.application.errors import UnauthorizedError
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal

USER = uuid.uuid4()
ALG = "HS256"


def run(coro):
    return asyncio.run(coro)


def claims_of(token: str) -> dict:
    return pyjwt.decode(token, settings.jwt_secret, algorithms=[ALG])


def forge(**payload) -> str:
    """A validly SIGNED token with attacker-chosen claims. Signature validity is
    not authorization — these must still be rejected on their contents."""
    base = {
        "sub": str(USER),
        "type": "access",
        "tenant_id": None,
        "roles": [],
        "iat": dt.datetime.now(dt.timezone.utc),
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
    }
    base.update(payload)
    return pyjwt.encode(base, settings.jwt_secret, algorithm=ALG)


# --- claim contract --------------------------------------------------------
def test_access_token_carries_exactly_the_declared_claims():
    token = JwtTokenService().create_access_token(USER, ["catalog_admin"])
    assert set(claims_of(token)) == set(ACCESS_TOKEN_CLAIMS)


def test_required_claims_are_present_and_correct():
    token = JwtTokenService().create_access_token(USER, ["support_agent"])
    payload = claims_of(token)
    assert payload["sub"] == str(USER)
    assert payload["type"] == "access"
    assert payload["roles"] == ["support_agent"]
    assert "tenant_id" in payload  # present even though null today


def test_roles_claim_is_populated_not_empty():
    """The defect this phase exists to fix: both issuance sites hardcoded
    `roles=[]`, so no token could ever carry authority."""
    token = JwtTokenService().create_access_token(USER, ["catalog_admin", "support_agent"])
    assert claims_of(token)["roles"] == ["catalog_admin", "support_agent"]


def test_tenant_id_is_emitted_when_supplied():
    tenant = uuid.uuid4()
    token = JwtTokenService().create_access_token(USER, [], tenant_id=tenant)
    assert claims_of(token)["tenant_id"] == str(tenant)
    assert JwtTokenService().decode_access(token).tenant_id == tenant


@pytest.mark.parametrize(
    "forbidden",
    ["permissions", "seller_ids", "sellers", "memberships",
     "mobile", "email", "full_name", "name", "phone"],
)
def test_forbidden_claims_never_appear(forbidden):
    """ADR-0011 §8: no permissions, no seller ids, no memberships, no PII."""
    token = JwtTokenService().create_access_token(USER, ["super_admin"])
    assert forbidden not in claims_of(token)


def test_no_claim_value_contains_a_permission_code():
    """Guards against a permission leaking in under an innocent claim name."""
    token = JwtTokenService().create_access_token(USER, ["super_admin"])
    blob = str(claims_of(token))
    assert not any(p.code in blob for p in Permission)


def test_refresh_token_is_not_accepted_as_an_access_token():
    raw, _hash = JwtTokenService().create_refresh_token(USER, uuid.uuid4())
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(raw)


# --- validation, all fail-closed -------------------------------------------
def test_valid_token_round_trips():
    token = JwtTokenService().create_access_token(USER, ["catalog_admin"])
    claims = JwtTokenService().decode_access(token)
    assert claims == AccessTokenClaims(USER, None, ("catalog_admin",))


def test_expired_token_is_rejected():
    token = forge(exp=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(token)


def test_wrong_signature_is_rejected():
    token = pyjwt.encode(
        {"sub": str(USER), "type": "access", "tenant_id": None, "roles": [],
         "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)},
        "not-the-real-secret", algorithm=ALG,
    )
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(token)


def test_alg_none_is_rejected():
    token = pyjwt.encode(
        {"sub": str(USER), "type": "access", "tenant_id": None, "roles": []},
        key="", algorithm="none",
    )
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(token)


@pytest.mark.parametrize("bad_type", ["refresh", "id", "", None, 123])
def test_invalid_token_type_is_rejected(bad_type):
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(forge(type=bad_type))


@pytest.mark.parametrize("bad_sub", ["not-a-uuid", "", None, 12345, []])
def test_invalid_subject_is_rejected(bad_sub):
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(forge(sub=bad_sub))


def test_missing_subject_is_rejected():
    token = pyjwt.encode(
        {"type": "access", "tenant_id": None, "roles": [],
         "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)},
        settings.jwt_secret, algorithm=ALG,
    )
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(token)


def test_missing_tenant_claim_is_rejected():
    """Presence is the contract, so a token minted before tenant scoping cannot
    silently pass once tenancy exists (ADR-0011 §8)."""
    token = pyjwt.encode(
        {"sub": str(USER), "type": "access", "roles": [],
         "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)},
        settings.jwt_secret, algorithm=ALG,
    )
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(token)


@pytest.mark.parametrize("bad_tenant", ["not-a-uuid", 42, [], {}])
def test_malformed_tenant_is_rejected(bad_tenant):
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(forge(tenant_id=bad_tenant))


@pytest.mark.parametrize(
    "bad_roles",
    ["catalog_admin", {"role": "admin"}, [1, 2], ["ok", 3], None, 7, [None]],
)
def test_malformed_roles_claim_fails_closed(bad_roles):
    """A structurally corrupt roles claim means the credential is untrustworthy:
    reject the token rather than silently reading zero roles from it."""
    with pytest.raises(UnauthorizedError):
        JwtTokenService().decode_access(forge(roles=bad_roles))


def test_absent_roles_claim_means_no_roles_not_an_error():
    """Distinct from malformed: a well-formed token with no roles is a valid
    credential carrying no platform authority."""
    token = pyjwt.encode(
        {"sub": str(USER), "type": "access", "tenant_id": None,
         "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)},
        settings.jwt_secret, algorithm=ALG,
    )
    assert JwtTokenService().decode_access(token).role_codes == ()


# --- Principal assembly ----------------------------------------------------
class FakeRoleGrants:
    """role code -> permission codes, as the database would answer."""

    def __init__(self, mapping: dict[str, set[str]]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[str, ...]] = []

    async def permission_codes_for_roles(self, role_codes):
        self.calls.append(tuple(role_codes))
        out: set[str] = set()
        for code in role_codes:
            out |= self.mapping.get(code, set())
        return frozenset(out)


CATALOG_ADMIN = {
    Permission.CATALOG_PRODUCTS_CREATE.code,
    Permission.CATALOG_PRODUCTS_UPDATE.code,
}
SUPPORT = {Permission.ORDERING_ORDERS_READ_ANY.code}


def assemble(role_codes, mapping=None, tenant=None) -> Principal:
    grants = FakeRoleGrants(mapping if mapping is not None
                            else {"catalog_admin": CATALOG_ADMIN, "support_agent": SUPPORT})
    factory = PrincipalFactory(role_grants=grants)
    return run(factory.from_claims(AccessTokenClaims(USER, tenant, tuple(role_codes))))


def test_platform_role_resolves_to_its_permissions():
    principal = assemble(["catalog_admin"])
    assert principal.can(Permission.CATALOG_PRODUCTS_CREATE)
    assert principal.can(Permission.CATALOG_PRODUCTS_UPDATE)
    assert not principal.can(Permission.ORDERING_ORDERS_READ_ANY)


def test_multiple_roles_union_their_permissions():
    principal = assemble(["catalog_admin", "support_agent"])
    assert principal.can(Permission.CATALOG_PRODUCTS_CREATE)
    assert principal.can(Permission.ORDERING_ORDERS_READ_ANY)


def test_no_roles_means_no_platform_permissions():
    principal = assemble([])
    assert principal.platform_permissions == frozenset()


def test_unknown_role_code_grants_nothing():
    principal = assemble(["wizard"])
    assert principal.platform_permissions == frozenset()


def test_unknown_permission_code_is_dropped_not_granted():
    """A database ahead of this build must not confer authority the code does not
    define (ADR-0011 §5, fail closed)."""
    principal = assemble(
        ["future_role"],
        mapping={"future_role": {
            "catalog.products.create", "quantum.teleport.everything",
        }},
    )
    assert principal.platform_permissions == {Permission.CATALOG_PRODUCTS_CREATE}


def test_assembled_principal_carries_the_subject_and_tenant():
    tenant = uuid.uuid4()
    principal = assemble(["catalog_admin"], tenant=tenant)
    assert principal.subject_id == USER
    assert principal.tenant_id == tenant


def test_assembled_principal_has_no_memberships_yet():
    """Phase 1C/1F supply memberships. Until then every seller-scoped question is
    denied, which is the correct direction to be wrong in."""
    principal = assemble(["catalog_admin"])
    assert principal.memberships == {}
    assert not principal.can_for_seller(
        Permission.SELLING_OFFERS_ACTIVATE, uuid.uuid4()
    )


def test_assembled_principal_is_immutable():
    import dataclasses

    principal = assemble(["catalog_admin"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.platform_permissions = frozenset()  # type: ignore[misc]


def test_no_roles_skips_the_grants_query_entirely():
    """The common case — a customer with no platform grants — costs no I/O."""
    grants = FakeRoleGrants({})
    factory = PrincipalFactory(role_grants=grants)
    run(factory.from_claims(AccessTokenClaims(USER, None, ())))
    assert grants.calls == [()]


def test_permissions_are_enum_members_not_strings():
    principal = assemble(["catalog_admin"])
    assert all(isinstance(p, Permission) for p in principal.platform_permissions)
    assert Permission.CATALOG_PRODUCTS_CREATE.code not in principal.platform_permissions
