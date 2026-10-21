"""Cache-first `MembershipQuery` with bounded database fallback (ADR-0011 §10).

Decorates `SqlMembershipQuery`. Callers see the port and cannot tell whether an
answer came from Redis or PostgreSQL — which is the property that lets Phase 1G
policies stay pure and lets the storage choice change later without touching
them.

    authz:memberships:{user_id}   seller_id -> member_role_code   5 min  ±10%
    authz:seller:{seller_id}      status/tenant/version          15 min  ±10%

Flat keys: no epoch component, deliberately (ADR-0011 §10.1). An epoch would
require a second Redis read on every request to discover the current epoch,
turning a one-GET path into two and defeating its own purpose. Invalidation is
therefore exact and per-key, and a seller suspension updates ONE seller key
rather than fanning out to every member.

**Negative caching is mandatory.** A user with no memberships caches an explicit
empty mapping; without that, the majority of users (plain customers) would miss
on every request and the cache would silently degrade into a per-request query.

**Nothing here fails open.** Every degraded path yields either fresh
authoritative data or no authority at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import Mapping, Sequence

from src.shared_kernel.application.security import (
    MembershipQuery,
    SellerAuthorizationState,
    SellerStatus,
)

logger = logging.getLogger("merchant.membership_cache")

MEMBERSHIP_KEY = "authz:memberships:{user_id}"
SELLER_KEY = "authz:seller:{seller_id}"

MEMBERSHIP_TTL_SECONDS = 300      # 5 minutes  (ADR-0011 §10.1)
SELLER_TTL_SECONDS = 900          # 15 minutes (ADR-0011 §10.2)
TTL_JITTER = 0.10                 # ±10%, so entries populated together do not
                                  # expire together

REDIS_TIMEOUT_SECONDS = 0.050     # §10.7
DB_TIMEOUT_SECONDS = 0.250
MAX_FALLBACK_CONCURRENCY = 5      # of a 30-connection pool: checkout can never
                                  # be starved by authorization


def jittered(ttl: int) -> int:
    """TTL ± TTL_JITTER, never below 1 second."""
    delta = ttl * TTL_JITTER
    return max(1, int(random.uniform(ttl - delta, ttl + delta)))


class CircuitBreaker:
    """Opens when Redis is failing, so a dead cache is not retried per request.

    50% errors over a 10s rolling window with a 20-sample minimum; open 5s; then
    half-open, where three consecutive successes close it and one failure
    reopens (ADR-0011 §10.7).
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(
        self, *, window_seconds: float = 10.0, minimum_samples: int = 20,
        failure_ratio: float = 0.5, open_seconds: float = 5.0,
        successes_to_close: int = 3,
    ) -> None:
        self._window = window_seconds
        self._minimum = minimum_samples
        self._ratio = failure_ratio
        self._open_for = open_seconds
        self._to_close = successes_to_close
        self._samples: list[tuple[float, bool]] = []   # (at, ok)
        self.state = self.CLOSED
        self._opened_at = 0.0
        self._consecutive_successes = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        self._samples = [s for s in self._samples if s[0] >= cutoff]

    def allows(self) -> bool:
        now = time.monotonic()
        if self.state == self.OPEN:
            if now - self._opened_at >= self._open_for:
                self.state = self.HALF_OPEN
                self._consecutive_successes = 0
                return True
            return False
        return True

    def record(self, ok: bool) -> None:
        now = time.monotonic()
        self._samples.append((now, ok))
        self._prune(now)

        if self.state == self.HALF_OPEN:
            if ok:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self._to_close:
                    self.state = self.CLOSED
                    self._samples.clear()
            else:
                self.state = self.OPEN
                self._opened_at = now
            return

        if len(self._samples) >= self._minimum:
            failures = sum(1 for _, sample_ok in self._samples if not sample_ok)
            if failures / len(self._samples) > self._ratio:
                self.state = self.OPEN
                self._opened_at = now


class CachedMembershipQuery:
    """The `MembershipQuery` the composition root actually uses."""

    def __init__(
        self,
        inner: MembershipQuery,
        redis: object | None,
        *,
        breaker: CircuitBreaker | None = None,
        max_fallback_concurrency: int = MAX_FALLBACK_CONCURRENCY,
    ) -> None:
        self._inner = inner
        self._redis = redis
        self._breaker = breaker if breaker is not None else CircuitBreaker()
        self._fallback = asyncio.Semaphore(max_fallback_concurrency)
        self._inflight: dict[str, asyncio.Future] = {}
        # Observability: R15/R16 both fail silently without these.
        self.stats = {
            "hit": 0, "miss": 0, "negative_hit": 0,
            "redis_error": 0, "fallback": 0, "fallback_exhausted": 0,
        }

    # -- Redis helpers ------------------------------------------------------
    async def _redis_get(self, key: str) -> str | None:
        if self._redis is None or not self._breaker.allows():
            return None
        try:
            value = await asyncio.wait_for(
                self._redis.get(key), timeout=REDIS_TIMEOUT_SECONDS  # type: ignore[attr-defined]
            )
            self._breaker.record(True)
            return value
        except Exception:                       # noqa: BLE001 - any failure is a miss
            self._breaker.record(False)
            self.stats["redis_error"] += 1
            return None

    async def _redis_set(self, key: str, value: str, ttl: int) -> None:
        if self._redis is None or not self._breaker.allows():
            return
        try:
            await asyncio.wait_for(
                self._redis.set(key, value, ex=jittered(ttl)),  # type: ignore[attr-defined]
                timeout=REDIS_TIMEOUT_SECONDS,
            )
            self._breaker.record(True)
        except Exception:                       # noqa: BLE001
            self._breaker.record(False)
            self.stats["redis_error"] += 1

    async def _single_flight(self, key: str, produce):
        """Collapse concurrent misses for one key into a single database read."""
        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await produce()
        except BaseException as exc:            # noqa: BLE001
            future.set_exception(exc)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    async def _bounded_db(self, produce):
        """Run a database read inside the fallback budget.

        Returns None when the budget is exhausted or the query times out. The
        caller then confers NO authority — denial, never an unchecked allow
        (ADR-0011 §10.7).
        """
        if self._fallback.locked() and self._fallback._value == 0:  # noqa: SLF001
            self.stats["fallback_exhausted"] += 1
            logger.warning("authorization fallback budget exhausted; denying seller authority")
            return None
        async with self._fallback:
            self.stats["fallback"] += 1
            try:
                return await asyncio.wait_for(produce(), timeout=DB_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self.stats["fallback_exhausted"] += 1
                logger.warning("authorization fallback query timed out; denying seller authority")
                return None

    # -- MembershipQuery ----------------------------------------------------
    async def memberships_for(self, user_id: uuid.UUID) -> Mapping[uuid.UUID, str]:
        key = MEMBERSHIP_KEY.format(user_id=user_id)
        cached = await self._redis_get(key)
        if cached is not None:
            payload = json.loads(cached)
            if payload:
                self.stats["hit"] += 1
            else:
                self.stats["negative_hit"] += 1   # the mandatory empty answer
            return {uuid.UUID(k): v for k, v in payload.items()}

        self.stats["miss"] += 1

        async def _load() -> Mapping[uuid.UUID, str] | None:
            return await self._bounded_db(lambda: self._inner.memberships_for(user_id))

        result = await self._single_flight(key, _load)
        if result is None:
            return {}                              # fail closed, and do NOT cache
        await self._redis_set(
            key, json.dumps({str(k): v for k, v in result.items()}),
            MEMBERSHIP_TTL_SECONDS,
        )
        return result

    async def seller_states(
        self, seller_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, SellerAuthorizationState]:
        if not seller_ids:
            return {}

        states: dict[uuid.UUID, SellerAuthorizationState] = {}
        missing: list[uuid.UUID] = []
        for seller_id in seller_ids:
            cached = await self._redis_get(SELLER_KEY.format(seller_id=seller_id))
            state = _decode_seller_state(cached) if cached else None
            if state is None:
                missing.append(seller_id)
            else:
                states[seller_id] = state

        if missing:
            fetched = await self._bounded_db(lambda: self._inner.seller_states(missing))
            if fetched is None:
                return states                      # fail closed for the unresolved ones
            for seller_id, state in fetched.items():
                states[seller_id] = state
                await self._write_seller_state(state)
        return states

    # -- cache maintenance --------------------------------------------------
    async def invalidate_user(self, user_id: uuid.UUID) -> None:
        """Exact, per-user invalidation, called after a membership mutation commits.

        Best-effort by design: the outbox-driven handler repeats it durably, and
        the TTL is the final bound (ADR-0011 §10.4/§10.5).
        """
        if self._redis is None:
            return
        try:
            await asyncio.wait_for(
                self._redis.delete(MEMBERSHIP_KEY.format(user_id=user_id)),  # type: ignore[attr-defined]
                timeout=REDIS_TIMEOUT_SECONDS,
            )
        except Exception:                        # noqa: BLE001
            self.stats["redis_error"] += 1
            logger.warning("membership cache invalidation failed for %s", user_id)

    async def apply_seller_state(self, state: SellerAuthorizationState) -> None:
        """Version-guarded seller-state write.

        An event carrying an older version than the cached one is DISCARDED,
        which is what makes out-of-order delivery safe (ADR-0011 §10.2). A
        suspension therefore updates one key, with no membership fan-out.
        """
        cached = await self._redis_get(SELLER_KEY.format(seller_id=state.seller_id))
        current = _decode_seller_state(cached) if cached else None
        if current is not None and current.version > state.version:
            return
        await self._write_seller_state(state)

    async def _write_seller_state(self, state: SellerAuthorizationState) -> None:
        await self._redis_set(
            SELLER_KEY.format(seller_id=state.seller_id),
            json.dumps({
                "seller_id": str(state.seller_id),
                "status": state.status.value,
                "tenant_id": str(state.tenant_id) if state.tenant_id else None,
                "version": state.version,
            }),
            SELLER_TTL_SECONDS,
        )


def _decode_seller_state(raw: str) -> SellerAuthorizationState | None:
    """Decode a cached seller state, or None if it is unusable.

    A malformed entry or an unknown status is treated as absent, so a corrupt or
    future-dated cache value confers nothing rather than being guessed at.
    """
    try:
        payload = json.loads(raw)
        return SellerAuthorizationState(
            seller_id=uuid.UUID(payload["seller_id"]),
            status=SellerStatus(payload["status"]),
            tenant_id=uuid.UUID(payload["tenant_id"]) if payload.get("tenant_id") else None,
            version=int(payload["version"]),
        )
    except Exception:                            # noqa: BLE001
        return None
