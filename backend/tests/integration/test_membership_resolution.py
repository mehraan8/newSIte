"""Membership resolution against real PostgreSQL and real Redis (Phase 1F).

The SQL filtering (`seller_members.status`/`sellers.status`), the join, the batch
seller-state query and the end-to-end Principal assembly are all exercised for
real, because the properties that matter here — that an inactive membership or a
suspended seller never reaches the application — live in the query, not in
Python.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.api.security import PrincipalFactory
from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.merchant.application.member_permissions import MEMBER_ROLE_PERMISSIONS
from src.modules.merchant.domain.entities import SellerMemberRole as R
from src.modules.merchant.infrastructure.membership_cache import (
    MEMBERSHIP_KEY,
    CachedMembershipQuery,
)
from src.modules.merchant.infrastructure.membership_query import SqlMembershipQuery
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import SellerStatus
from tests.integration import db

db.skip_if_unavailable()

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def schema():
    db.migrate()
    yield


@pytest.fixture(autouse=True)
def clean():
    def _wipe():
        db.execute("delete from seller_members")
        db.execute("delete from sellers")

    _wipe()
    yield
    _wipe()


# --- fixtures ---------------------------------------------------------------
def make_seller(status: str = "active", tenant=None, version: int = 1) -> uuid.UUID:
    seller_id = uuid.uuid4()
    db.execute(
        "insert into sellers (id, code, name, status, tenant_id, version) "
        "values (:i,:c,'S',:s,:t,:v)",
        i=seller_id, c=f"s-{uuid.uuid4().hex[:10]}", s=status, t=tenant, v=version,
    )
    return seller_id


def add_member(seller_id, user_id, role=R.OWNER, status="active") -> None:
    db.execute(
        "insert into seller_members (seller_id, user_id, member_role_code, status) "
        "values (:s,:u,:r,:st)",
        s=seller_id, u=user_id, r=role.value if hasattr(role, "value") else role,
        st=status,
    )


def query():
    """SqlMembershipQuery on a real session."""
    async def _go(fn):
        async with db.session_factory()() as session:
            return await fn(SqlMembershipQuery(session))
    return _go


def memberships_of(user_id):
    return db.run(query()(lambda q: q.memberships_for(user_id)))


def states_of(seller_ids):
    return db.run(query()(lambda q: q.seller_states(seller_ids)))


def principal_for(user_id, roles=()):
    """Full assembly through the real adapters."""
    async def _go():
        async with db.session_factory()() as session:
            class NoGrants:
                async def permission_codes_for_roles(self, role_codes):
                    return frozenset()
            factory = PrincipalFactory(
                NoGrants(), memberships=SqlMembershipQuery(session)
            )
            return await factory.from_claims(
                AccessTokenClaims(user_id, None, tuple(roles))
            )
    return db.run(_go())


# --- membership query -------------------------------------------------------
def test_user_with_no_memberships_resolves_empty():
    assert memberships_of(uuid.uuid4()) == {}


def test_single_active_membership_resolves():
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.MANAGER)
    assert memberships_of(user) == {seller: "seller_manager"}


def test_user_belonging_to_two_sellers_gets_both():
    user = uuid.uuid4()
    a, b = make_seller(), make_seller()
    add_member(a, user, R.OWNER)
    add_member(b, user, R.STAFF)
    assert memberships_of(user) == {a: "seller_owner", b: "seller_staff"}


def test_two_users_in_one_seller_resolve_independently():
    seller = make_seller()
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    add_member(seller, u1, R.OWNER)
    add_member(seller, u2, R.VIEWER)
    assert memberships_of(u1) == {seller: "seller_owner"}
    assert memberships_of(u2) == {seller: "seller_viewer"}


def test_inactive_membership_is_filtered_out_in_sql():
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.OWNER, status="suspended")
    assert memberships_of(user) == {}


@pytest.mark.parametrize("seller_status", ["suspended", "pending"])
def test_non_active_seller_is_filtered_out_in_sql(seller_status):
    user, seller = uuid.uuid4(), make_seller(status=seller_status)
    add_member(seller, user, R.OWNER)
    assert memberships_of(user) == {}


def test_only_the_active_pair_survives_a_mixed_set():
    user = uuid.uuid4()
    ok = make_seller()
    inactive_member = make_seller()
    suspended_seller = make_seller(status="suspended")
    add_member(ok, user, R.MANAGER)
    add_member(inactive_member, user, R.OWNER, status="suspended")
    add_member(suspended_seller, user, R.OWNER)
    assert memberships_of(user) == {ok: "seller_manager"}


def test_seller_states_are_batched_not_n_plus_one():
    sellers = [make_seller() for _ in range(5)]
    states = states_of(sellers)
    assert set(states) == set(sellers)
    assert all(s.status is SellerStatus.ACTIVE for s in states.values())


def test_seller_states_omits_unknown_ids():
    known = make_seller()
    states = states_of([known, uuid.uuid4()])
    assert set(states) == {known}


def test_seller_state_carries_tenant_and_version():
    tenant = uuid.uuid4()
    seller = make_seller(tenant=tenant, version=7)
    state = states_of([seller])[seller]
    assert state.tenant_id == tenant and state.version == 7


def test_empty_seller_id_list_issues_no_query():
    assert states_of([]) == {}


# --- Principal assembly, end to end ----------------------------------------
def test_assembly_produces_a_complete_grant():
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.MANAGER)

    principal = principal_for(user)

    grant = principal.memberships[seller]
    assert grant.seller_id == seller
    assert grant.status is SellerStatus.ACTIVE
    assert grant.permissions == MEMBER_ROLE_PERMISSIONS[R.MANAGER]
    assert principal.can_for_seller(Permission.SELLING_OFFERS_PRICE_CHANGE, seller)


def test_staff_cannot_change_price_end_to_end():
    """Decision A, proven through the database rather than the matrix alone."""
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.STAFF)

    principal = principal_for(user)

    assert principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, seller)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_PRICE_CHANGE, seller)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_ACTIVATE, seller)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_SUSPEND, seller)


def test_viewer_can_read_but_not_mutate_end_to_end():
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.VIEWER)
    principal = principal_for(user)
    assert principal.can_for_seller(Permission.SELLING_OFFERS_READ_OWN, seller)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, seller)


def test_only_owner_manages_members_end_to_end():
    seller = make_seller()
    owner, manager = uuid.uuid4(), uuid.uuid4()
    add_member(seller, owner, R.OWNER)
    add_member(seller, manager, R.MANAGER)
    assert principal_for(owner).can_for_seller(Permission.MERCHANT_MEMBERS_INVITE, seller)
    assert not principal_for(manager).can_for_seller(
        Permission.MERCHANT_MEMBERS_INVITE, seller
    )


def test_suspended_seller_denies_a_warm_membership():
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.OWNER)
    assert principal_for(user).can_for_seller(Permission.SELLING_OFFERS_CREATE, seller)

    db.execute("update sellers set status='suspended' where id=:i", i=seller)

    principal = principal_for(user)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, seller)


CK_MEMBER_ROLE = (
    "check (member_role_code in "
    "('seller_owner','seller_manager','seller_staff','seller_viewer'))"
)


def test_unknown_member_role_in_the_database_fails_closed():
    """Models the rollback case: the database holds a role code that the DEPLOYED
    build no longer defines.

    The CHECK constraint prevents such a row normally — which is defence in depth
    worth having — so it is briefly lifted to create the condition. Both the drop
    and the restore are idempotent, so a failure mid-test cannot leave the schema
    damaged for the tests that follow.
    """
    user, seller = uuid.uuid4(), make_seller()
    db.execute("alter table seller_members drop constraint if exists ck_seller_member_role")
    try:
        add_member(seller, user, "wizard")
        assert memberships_of(user) == {seller: "wizard"}      # SQL is faithful
        assert principal_for(user).memberships == {}           # assembly is not
    finally:
        db.execute("delete from seller_members where member_role_code = 'wizard'")
        db.execute(
            "do $$ begin "
            "if not exists (select 1 from pg_constraint "
            "               where conname='ck_seller_member_role') then "
            f"  alter table seller_members add constraint ck_seller_member_role {CK_MEMBER_ROLE}; "
            "end if; end $$;"
        )


def test_the_member_role_check_constraint_is_intact():
    """Guards the test above: if it ever leaves the schema damaged, this fails."""
    names = {
        r["conname"] for r in db.rows(
            "select conname from pg_constraint "
            "where conrelid='seller_members'::regclass and contype='c'"
        )
    }
    assert "ck_seller_member_role" in names


def test_tenant_on_the_grant_comes_from_the_seller():
    tenant = uuid.uuid4()
    user, seller = uuid.uuid4(), make_seller(tenant=tenant)
    add_member(seller, user, R.OWNER)
    principal = principal_for(user)
    assert principal.memberships[seller].tenant_id == tenant
    assert principal.tenant_id is None


def test_a_users_own_id_never_becomes_a_seller_membership():
    user = uuid.uuid4()
    make_seller()                       # a seller exists, unrelated to this user
    principal = principal_for(user)
    assert principal.memberships == {}
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, user)


def test_platform_role_does_not_create_membership():
    user, seller = uuid.uuid4(), make_seller()

    async def _go():
        async with db.session_factory()() as session:
            class SuperAdmin:
                async def permission_codes_for_roles(self, role_codes):
                    return frozenset({Permission.SELLING_OFFERS_MANAGE_ANY.code})
            factory = PrincipalFactory(SuperAdmin(), memberships=SqlMembershipQuery(session))
            return await factory.from_claims(
                AccessTokenClaims(user, None, ("super_admin",))
            )

    principal = db.run(_go())
    assert principal.can(Permission.SELLING_OFFERS_MANAGE_ANY)
    assert principal.memberships == {}
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_MANAGE_ANY, seller)


# --- real Redis -------------------------------------------------------------
def redis_client():
    from src.shared_kernel.infrastructure.redis_client import get_redis
    return get_redis()


def test_real_redis_caches_and_serves_memberships():
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.OWNER)

    async def _go():
        redis = redis_client()
        await redis.delete(MEMBERSHIP_KEY.format(user_id=user))
        async with db.session_factory()() as session:
            cache = CachedMembershipQuery(SqlMembershipQuery(session), redis)
            first = await cache.memberships_for(user)
            # Remove the row: a second identical answer can now only be a cache hit.
            await session.execute(
                __import__("sqlalchemy").text(
                    "delete from seller_members where user_id = :u"
                ), {"u": str(user)},
            )
            await session.commit()
            second = await cache.memberships_for(user)
            await redis.delete(MEMBERSHIP_KEY.format(user_id=user))
            return first, second, cache.stats

    first, second, stats = db.run(_go())
    assert first == {seller: "seller_owner"}
    assert second == first, "second read must come from Redis"
    assert stats["hit"] == 1


def test_real_redis_stores_the_negative_answer():
    user = uuid.uuid4()

    async def _go():
        redis = redis_client()
        key = MEMBERSHIP_KEY.format(user_id=user)
        await redis.delete(key)
        async with db.session_factory()() as session:
            cache = CachedMembershipQuery(SqlMembershipQuery(session), redis)
            await cache.memberships_for(user)
            stored = await redis.get(key)
            ttl = await redis.ttl(key)
            await redis.delete(key)
            return stored, ttl, cache.stats

    stored, ttl, stats = db.run(_go())
    assert stored == "{}", "an empty membership set must be cached"
    assert 270 <= ttl <= 330
    assert stats["miss"] == 1


def test_real_redis_invalidation_forces_a_refetch():
    user, seller = uuid.uuid4(), make_seller()
    add_member(seller, user, R.STAFF)

    async def _go():
        redis = redis_client()
        async with db.session_factory()() as session:
            cache = CachedMembershipQuery(SqlMembershipQuery(session), redis)
            await cache.memberships_for(user)
            await cache.invalidate_user(user)
            present = await redis.get(MEMBERSHIP_KEY.format(user_id=user))
            await cache.memberships_for(user)
            await redis.delete(MEMBERSHIP_KEY.format(user_id=user))
            return present, cache.stats

    present, stats = db.run(_go())
    assert present is None, "invalidation must remove the key"
    assert stats["miss"] == 2, "the refetch must reach the database again"
