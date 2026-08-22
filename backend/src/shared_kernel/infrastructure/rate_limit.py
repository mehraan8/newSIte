"""Redis-backed fixed-window rate limiting for abuse-prone endpoints.

Redis, not memory, because the application runs as N processes behind a load
balancer: an in-process counter caps each worker separately, so the real limit is
N times what it claims and vanishes entirely on a restart. Coordination has to
live where all the workers can see it, and Redis is already a hard dependency.

FAIL-CLOSED. If Redis cannot be reached the request is REFUSED, not waved
through. That is the opposite of the membership cache (ADR-0011 §10.7), and
deliberately so: there, failing open costs a slower request and a stale
permission; here, failing open removes the only thing standing between an
attacker and unlimited OTP sends or password-guessing. A rate limiter that
disables itself under load is not a rate limiter — load is exactly what an
attacker creates.

Fixed window rather than sliding: one INCR plus one EXPIRE, evaluated
server-side, no read-modify-write and therefore no race between concurrent
requests. The known cost is burst tolerance at a window boundary (up to 2x the
limit across two adjacent windows), which is acceptable for abuse control where
the goal is bounding volume, not exact fairness.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from src.shared_kernel.application.errors import RateLimitedError

logger = logging.getLogger("security.rate_limit")

KEY = "ratelimit:{scope}:{identity}:{window}"


class RateLimiter:
    """Counts requests per (scope, identity) in fixed windows."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(
        self, scope: str, identity: str, *, limit: int, window_seconds: int
    ) -> int:
        """Record one request and return the count so far in this window.

        Raises `RateLimitedError` (429) once the count exceeds `limit`, and on any
        Redis failure.
        """
        # The window number is derived from the clock, so every worker computes
        # the same bucket without coordinating.
        import time

        window = int(time.time()) // window_seconds
        key = KEY.format(scope=scope, identity=identity, window=window)

        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                # Set on every hit rather than only the first: an EXPIRE that
                # failed once would otherwise leave a key that never dies and a
                # counter that never resets, locking the identity out forever.
                pipe.expire(key, window_seconds)
                count, _ = await pipe.execute()
        except Exception as exc:  # noqa: BLE001 - any Redis fault must deny
            logger.error("rate limiter unavailable for scope=%s: %s", scope, exc)
            raise RateLimitedError(
                "سرویس موقتاً در دسترس نیست. لطفاً بعداً تلاش کنید."
            ) from exc

        if int(count) > limit:
            # Deliberately identical to the message above: whether a caller was
            # limited or the limiter was down is not their business, and the
            # difference would tell an attacker when to push harder.
            logger.warning(
                "rate limit exceeded scope=%s identity=%s count=%s limit=%s",
                scope, identity, count, limit,
            )
            raise RateLimitedError(
                "تعداد درخواست‌ها بیش از حد مجاز است. لطفاً بعداً تلاش کنید."
            )
        return int(count)


def client_identity(request) -> str:
    """A best-effort caller identifier for per-client limits.

    `request.client.host` only, NOT `X-Forwarded-For`: that header is
    caller-supplied, so trusting it hands an attacker a free limit reset on every
    request — they simply vary the value. Behind a proxy this must be replaced by
    the proxy's verified client address (e.g. uvicorn `--proxy-headers` with
    `--forwarded-allow-ips`), which is a deployment concern, not a code one.

    Callers combine this with a per-subject limit, so a single shared NAT address
    does not become the only control.
    """
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"
