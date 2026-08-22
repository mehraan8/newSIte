"""Membership cache semantics and Principal assembly of memberships (Phase 1F).

DB-free: a fake `MembershipQuery` stands in for PostgreSQL and a fake Redis for
the cache, so every branch — including the ones a healthy system never takes
(Redis down, breaker open, fallback exhausted, out-of-order events) — is
exercised deterministically. Real PostgreSQL and real Redis are covered by
`tests/integration/test_membership_resolution.py`.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from src.api.security import PrincipalFactory
from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.merchant.application.member_permissions import MEMBER_ROLE_PERMISSIONS
from src.modules.merchant.domain.entities import SellerMemberRole as R
from src.modules.merchant.infrastructure.membership_cache import (
    MEMBERSHIP_KEY,
    MEMBERSHIP_TTL_SECONDS,
    SELLER_KEY,
    SELLER_TTL_SECONDS,
    CachedMembershipQuery,
    CircuitBreaker,
    jittered,
)
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import (
    SellerAuthorizationState,
    SellerStatus,
)

USER = uuid.uuid4()
SELLER_A = uuid.uuid4()
SELLER_B = uuid.uuid4()


def run(coro):
    return asyncio.run(coro)


class FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail
        self.gets = 0
        self.sets = 0

    async def get(self, key):
        self.gets += 1
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.sets += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = value
        self.ttls[key] = ex

    async def delete(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.store.pop(key, None)


class FakeMembershipQuery:
    """Stands in for SqlMembershipQuery. Counts calls so cache hits are provable."""

    def __init__(self, memberships=None, states=None, *, delay: float = 0.0) -> None:
        self._memberships = memberships or {}
        self._states = states or {}
        self.membership_calls = 0
        self.state_calls = 0
        self.delay = delay

    async def memberships_for(self, user_id):
        self.membership_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return dict(self._memberships)

    async def seller_states(self, seller_ids):
        self.state_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {s: self._states[s] for s in seller_ids if s in self._states}


def active(seller_id, version=1, tenant=None):
    return SellerAuthorizationState(seller_id, SellerStatus.ACTIVE, tenant, version)


# --- membership cache -------------------------------------------------------
def test_positive_membership_is_cached_and_reused():
    inner = FakeMembershipQuery({SELLER_A: R.OWNER.value})
    redis = FakeRedis()
    cache = CachedMembershipQuery(inner, redis)

    first = run(cache.memberships_for(USER))
    second = run(cache.memberships_for(USER))

    assert first == second == {SELLER_A: R.OWNER.value}
    assert inner.membership_calls == 1, "second call must be served from cache"
    assert cache.stats["hit"] == 1 and cache.stats["miss"] == 1


def test_negative_result_is_cached():
    """Mandatory: without this, every membership-less customer misses forever and
    the cache silently degrades into a per-request query (ADR-0011 §10.1)."""
    inner = FakeMembershipQuery({})
    redis = FakeRedis()
    cache = CachedMembershipQuery(inner, redis)

    assert run(cache.memberships_for(USER)) == {}
    assert run(cache.memberships_for(USER)) == {}
    assert inner.membership_calls == 1
    assert cache.stats["negative_hit"] == 1
    assert MEMBERSHIP_KEY.format(user_id=USER) in redis.store


def test_membership_key_is_flat_with_no_epoch():
    key = MEMBERSHIP_KEY.format(user_id=USER)
    assert key == f"authz:memberships:{USER}"
    assert "v" not in key.split(":")[1]


def test_membership_ttl_is_five_minutes_with_jitter():
    inner = FakeMembershipQuery({SELLER_A: R.STAFF.value})
    redis = FakeRedis()
    run(CachedMembershipQuery(inner, redis).memberships_for(USER))
    ttl = redis.ttls[MEMBERSHIP_KEY.format(user_id=USER)]
    assert MEMBERSHIP_TTL_SECONDS == 300
    assert 270 <= ttl <= 330


def test_jitter_spreads_expiry_and_stays_within_ten_percent():
    values = {jittered(300) for _ in range(200)}
    assert len(values) > 1, "TTL must not be constant"
    assert all(270 <= v <= 330 for v in values)


def test_invalidation_removes_the_user_key_only():
    inner = FakeMembershipQuery({SELLER_A: R.OWNER.value})
    redis = FakeRedis()
    cache = CachedMembershipQuery(inner, redis)
    run(cache.memberships_for(USER))
    redis.store[SELLER_KEY.format(seller_id=SELLER_A)] = "{}"

    run(cache.invalidate_user(USER))

    assert MEMBERSHIP_KEY.format(user_id=USER) not in redis.store
    assert SELLER_KEY.format(seller_id=SELLER_A) in redis.store, (
        "suspension must not fan out; only the user's key is invalidated"
    )
    run(cache.memberships_for(USER))
    assert inner.membership_calls == 2


def test_single_flight_collapses_concurrent_misses():
    inner = FakeMembershipQuery({SELLER_A: R.OWNER.value}, delay=0.02)
    cache = CachedMembershipQuery(inner, FakeRedis())

    async def _go():
        return await asyncio.gather(*(cache.memberships_for(USER) for _ in range(8)))

    results = run(_go())
    assert all(r == {SELLER_A: R.OWNER.value} for r in results)
    assert inner.membership_calls == 1, "8 concurrent misses must be one query"


# --- seller-state cache -----------------------------------------------------
def test_seller_state_is_cached_with_fifteen_minute_ttl():
    inner = FakeMembershipQuery({}, {SELLER_A: active(SELLER_A)})
    redis = FakeRedis()
    cache = CachedMembershipQuery(inner, redis)

    run(cache.seller_states([SELLER_A]))
    run(cache.seller_states([SELLER_A]))

    assert inner.state_calls == 1
    ttl = redis.ttls[SELLER_KEY.format(seller_id=SELLER_A)]
    assert SELLER_TTL_SECONDS == 900
    assert 810 <= ttl <= 990


def test_seller_state_write_is_version_guarded():
    """Out-of-order delivery is made safe by discarding an older version."""
    redis = FakeRedis()
    cache = CachedMembershipQuery(FakeMembershipQuery(), redis)

    run(cache.apply_seller_state(
        SellerAuthorizationState(SELLER_A, SellerStatus.SUSPENDED, None, 5)))
    run(cache.apply_seller_state(
        SellerAuthorizationState(SELLER_A, SellerStatus.ACTIVE, None, 3)))  # older

    stored = json.loads(redis.store[SELLER_KEY.format(seller_id=SELLER_A)])
    assert stored["version"] == 5 and stored["status"] == "suspended"


def test_a_newer_version_is_applied():
    redis = FakeRedis()
    cache = CachedMembershipQuery(FakeMembershipQuery(), redis)
    run(cache.apply_seller_state(
        SellerAuthorizationState(SELLER_A, SellerStatus.ACTIVE, None, 1)))
    run(cache.apply_seller_state(
        SellerAuthorizationState(SELLER_A, SellerStatus.SUSPENDED, None, 2)))
    stored = json.loads(redis.store[SELLER_KEY.format(seller_id=SELLER_A)])
    assert stored["version"] == 2 and stored["status"] == "suspended"


def test_seller_suspension_is_one_key_and_needs_no_membership_fanout():
    redis = FakeRedis()
    cache = CachedMembershipQuery(FakeMembershipQuery(), redis)
    redis.store[MEMBERSHIP_KEY.format(user_id=USER)] = json.dumps({str(SELLER_A): "seller_owner"})

    run(cache.apply_seller_state(
        SellerAuthorizationState(SELLER_A, SellerStatus.SUSPENDED, None, 2)))

    assert MEMBERSHIP_KEY.format(user_id=USER) in redis.store, (
        "membership caches stay warm; the seller key alone carries the suspension"
    )


def test_corrupt_or_unknown_cached_seller_state_is_treated_as_absent():
    inner = FakeMembershipQuery({}, {SELLER_A: active(SELLER_A)})
    redis = FakeRedis()
    redis.store[SELLER_KEY.format(seller_id=SELLER_A)] = '{"status": "wat"}'
    cache = CachedMembershipQuery(inner, redis)
    states = run(cache.seller_states([SELLER_A]))
    assert states[SELLER_A].status is SellerStatus.ACTIVE   # refetched, not guessed
    assert inner.state_calls == 1


# --- degraded paths never fail open ----------------------------------------
def test_redis_outage_falls_back_to_the_database():
    inner = FakeMembershipQuery({SELLER_A: R.OWNER.value})
    cache = CachedMembershipQuery(inner, FakeRedis(fail=True))
    assert run(cache.memberships_for(USER)) == {SELLER_A: R.OWNER.value}
    assert cache.stats["redis_error"] > 0


def test_no_redis_configured_still_resolves_from_the_database():
    inner = FakeMembershipQuery({SELLER_A: R.STAFF.value})
    cache = CachedMembershipQuery(inner, None)
    assert run(cache.memberships_for(USER)) == {SELLER_A: R.STAFF.value}


def test_fallback_exhaustion_confers_no_authority():
    """Denial, never an unchecked allow (ADR-0011 §10.7)."""
    class Hanging:
        membership_calls = 0
        async def memberships_for(self, user_id):
            await asyncio.sleep(5)
        async def seller_states(self, seller_ids):
            return {}

    cache = CachedMembershipQuery(Hanging(), None)
    import src.modules.merchant.infrastructure.membership_cache as mod
    original = mod.DB_TIMEOUT_SECONDS
    mod.DB_TIMEOUT_SECONDS = 0.01
    try:
        assert run(cache.memberships_for(USER)) == {}
    finally:
        mod.DB_TIMEOUT_SECONDS = original
    assert cache.stats["fallback_exhausted"] == 1


def test_a_timed_out_lookup_fails_closed_without_poisoning_the_cache():
    """A degraded read denies, and must NOT be stored as a false negative —
    otherwise one blip would deny a real member for the whole TTL."""
    class Timing_out:
        async def memberships_for(self, user_id):
            raise TimeoutError          # is asyncio.TimeoutError on 3.11+
        async def seller_states(self, seller_ids):
            return {}

    redis = FakeRedis()
    cache = CachedMembershipQuery(Timing_out(), redis)
    assert run(cache.memberships_for(USER)) == {}
    assert MEMBERSHIP_KEY.format(user_id=USER) not in redis.store


def test_an_unexpected_error_propagates_rather_than_silently_denying():
    """Only the bounded-fallback conditions degrade to a denial. A genuine bug
    must surface, not be laundered into 'this user has no memberships'."""
    class Broken:
        async def memberships_for(self, user_id):
            raise ValueError("bug")
        async def seller_states(self, seller_ids):
            return {}

    redis = FakeRedis()
    cache = CachedMembershipQuery(Broken(), redis)
    with pytest.raises(ValueError):
        run(cache.memberships_for(USER))
    assert MEMBERSHIP_KEY.format(user_id=USER) not in redis.store


# --- circuit breaker --------------------------------------------------------
def test_breaker_opens_after_sustained_failure():
    breaker = CircuitBreaker(minimum_samples=20, failure_ratio=0.5)
    for _ in range(20):
        breaker.record(False)
    assert breaker.state == CircuitBreaker.OPEN
    assert not breaker.allows()


def test_breaker_stays_closed_below_the_sample_minimum():
    breaker = CircuitBreaker(minimum_samples=20)
    for _ in range(19):
        breaker.record(False)
    assert breaker.state == CircuitBreaker.CLOSED and breaker.allows()


def test_breaker_half_opens_then_closes_after_three_successes():
    breaker = CircuitBreaker(minimum_samples=20, open_seconds=0.0, successes_to_close=3)
    for _ in range(20):
        breaker.record(False)
    assert breaker.state == CircuitBreaker.OPEN
    assert breaker.allows()                       # probe admitted
    assert breaker.state == CircuitBreaker.HALF_OPEN
    breaker.record(True); breaker.record(True); breaker.record(True)
    assert breaker.state == CircuitBreaker.CLOSED


def test_one_failure_reopens_a_half_open_breaker():
    breaker = CircuitBreaker(minimum_samples=20, open_seconds=0.0)
    for _ in range(20):
        breaker.record(False)
    breaker.allows()
    breaker.record(False)
    assert breaker.state == CircuitBreaker.OPEN


def test_open_breaker_skips_redis_entirely():
    inner = FakeMembershipQuery({SELLER_A: R.OWNER.value})
    redis = FakeRedis()
    breaker = CircuitBreaker(minimum_samples=1, failure_ratio=0.0, open_seconds=60)
    breaker.record(False)
    cache = CachedMembershipQuery(inner, redis, breaker=breaker)
    assert run(cache.memberships_for(USER)) == {SELLER_A: R.OWNER.value}
    assert redis.gets == 0, "an open breaker must not touch Redis"


# --- Principal assembly -----------------------------------------------------
def assemble(memberships, states, roles=()):
    class NoGrants:
        async def permission_codes_for_roles(self, role_codes):
            return frozenset()

    factory = PrincipalFactory(
        NoGrants(), memberships=FakeMembershipQuery(memberships, states)
    )
    return run(factory.from_claims(AccessTokenClaims(USER, None, tuple(roles))))


def test_active_membership_in_active_seller_produces_a_usable_grant():
    principal = assemble({SELLER_A: R.MANAGER.value}, {SELLER_A: active(SELLER_A)})
    assert set(principal.memberships) == {SELLER_A}
    assert principal.can_for_seller(Permission.SELLING_OFFERS_PRICE_CHANGE, SELLER_A)
    assert principal.memberships[SELLER_A].permissions == MEMBER_ROLE_PERMISSIONS[R.MANAGER]


def test_multiple_memberships_resolve_independently():
    principal = assemble(
        {SELLER_A: R.OWNER.value, SELLER_B: R.VIEWER.value},
        {SELLER_A: active(SELLER_A), SELLER_B: active(SELLER_B)},
    )
    assert principal.can_for_seller(Permission.SELLING_OFFERS_PRICE_CHANGE, SELLER_A)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_PRICE_CHANGE, SELLER_B)
    assert principal.can_for_seller(Permission.SELLING_OFFERS_READ_OWN, SELLER_B)


def test_one_sellers_state_cannot_affect_another():
    principal = assemble(
        {SELLER_A: R.OWNER.value, SELLER_B: R.OWNER.value},
        {
            SELLER_A: active(SELLER_A),
            SELLER_B: SellerAuthorizationState(SELLER_B, SellerStatus.SUSPENDED, None, 1),
        },
    )
    assert principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, SELLER_A)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, SELLER_B)


@pytest.mark.parametrize("status", [SellerStatus.SUSPENDED, SellerStatus.PENDING])
def test_non_active_seller_confers_no_authority(status):
    principal = assemble(
        {SELLER_A: R.OWNER.value},
        {SELLER_A: SellerAuthorizationState(SELLER_A, status, None, 1)},
    )
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, SELLER_A)


def test_missing_seller_state_produces_no_grant():
    principal = assemble({SELLER_A: R.OWNER.value}, {})
    assert principal.memberships == {}


def test_unknown_member_role_code_is_dropped():
    principal = assemble({SELLER_A: "wizard"}, {SELLER_A: active(SELLER_A)})
    assert principal.memberships == {}


def test_a_known_membership_survives_alongside_an_unknown_one():
    principal = assemble(
        {SELLER_A: R.STAFF.value, SELLER_B: "wizard"},
        {SELLER_A: active(SELLER_A), SELLER_B: active(SELLER_B)},
    )
    assert set(principal.memberships) == {SELLER_A}


def test_tenant_comes_from_the_seller_not_the_user():
    tenant = uuid.uuid4()
    principal = assemble(
        {SELLER_A: R.OWNER.value}, {SELLER_A: active(SELLER_A, tenant=tenant)}
    )
    assert principal.memberships[SELLER_A].tenant_id == tenant
    assert principal.tenant_id is None          # the token carried none


def test_no_memberships_means_no_seller_authority():
    principal = assemble({}, {})
    assert principal.memberships == {}
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, SELLER_A)


def test_subject_id_remains_the_user_and_is_never_a_seller_key():
    principal = assemble({SELLER_A: R.OWNER.value}, {SELLER_A: active(SELLER_A)})
    assert principal.subject_id == USER
    assert USER not in principal.memberships, (
        "a user's own id must never appear as a seller membership"
    )
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_CREATE, USER)


def test_membership_confers_no_platform_permission():
    principal = assemble({SELLER_A: R.OWNER.value}, {SELLER_A: active(SELLER_A)})
    assert principal.platform_permissions == frozenset()
    for permission in principal.memberships[SELLER_A].permissions:
        assert not principal.can(permission)


def test_platform_permission_confers_no_membership():
    """Even a platform-wide override does not make anyone a member."""
    class SuperAdmin:
        async def permission_codes_for_roles(self, role_codes):
            return frozenset({Permission.SELLING_OFFERS_MANAGE_ANY.code})

    factory = PrincipalFactory(SuperAdmin(), memberships=FakeMembershipQuery({}, {}))
    principal = run(factory.from_claims(AccessTokenClaims(USER, None, ("super_admin",))))

    assert principal.can(Permission.SELLING_OFFERS_MANAGE_ANY)
    assert principal.memberships == {}
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_MANAGE_ANY, SELLER_A)
    assert not principal.can_for_seller(Permission.SELLING_OFFERS_ACTIVATE, SELLER_A)


def test_factory_without_a_membership_query_stays_fail_closed():
    class NoGrants:
        async def permission_codes_for_roles(self, role_codes):
            return frozenset()

    principal = run(PrincipalFactory(NoGrants()).from_claims(
        AccessTokenClaims(USER, None, ())
    ))
    assert principal.memberships == {}
