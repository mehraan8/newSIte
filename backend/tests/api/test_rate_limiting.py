"""Abuse protection on the unauthenticated auth surface — audit regression lock.

Before this, the audit sent 40 OTP requests to 40 different numbers from one
client and all 40 were accepted, and 30 refresh attempts drew no limit at all.
The per-mobile cooldown that existed bounded messages to ONE number, which does
nothing against an attacker who simply varies the number — the exact shape of an
SMS-bombing or SMS-cost attack.

Real Redis, real middleware, real endpoints. `tests/api/conftest.py` clears the
counters between tests so the budgets are per test rather than per file; the
limiter itself is never disabled, and the tests below prove it fires.
"""

from __future__ import annotations

import uuid

import pytest

from src.config.settings import settings
from src.shared_kernel.application.errors import RateLimitedError
from src.shared_kernel.infrastructure.rate_limit import RateLimiter, client_identity
from src.shared_kernel.infrastructure.redis_client import get_redis
from tests.api.conftest import call
from tests.integration import db

pytestmark = pytest.mark.integration

OTP_REQUEST = "/api/v1/auth/otp/request"
OTP_VERIFY = "/api/v1/auth/otp/verify"
REFRESH = "/api/v1/auth/token/refresh"


def mobile() -> str:
    return "+989" + str(uuid.uuid4().int)[:9]


def test_otp_requests_are_capped_per_client_across_different_numbers(client):
    """THE finding. Varying the target number must not buy an unlimited budget.

    Each request costs the operator a real SMS, so an uncapped endpoint is a
    direct billing attack — and points third parties who never used the site at
    an SMS flood they cannot stop.
    """
    limit = settings.otp_request_ip_limit
    statuses = [
        call(client, "POST", OTP_REQUEST, json={"mobile": mobile()}).status_code
        for _ in range(limit + 5)
    ]

    assert statuses[:limit] == [201] * limit, statuses
    assert set(statuses[limit:]) == {429}, statuses


def test_otp_verification_is_capped_per_client(client):
    """The per-challenge attempt cap resets whenever a new code is requested, so
    an attacker cycling request/verify gets unlimited guesses without this."""
    target = mobile()
    call(client, "POST", OTP_REQUEST, json={"mobile": target})

    limit = settings.otp_verify_ip_limit
    statuses = [
        call(client, "POST", OTP_VERIFY,
             json={"mobile": target, "code": f"{i:06d}"}).status_code
        for i in range(limit + 3)
    ]

    assert 429 in statuses
    assert statuses[-1] == 429, statuses


def test_refresh_attempts_are_capped_per_client(client):
    """A refresh token is unguessable, so this is a resource ceiling rather than
    an anti-guessing one: every attempt costs a database round trip."""
    limit = settings.refresh_ip_limit
    statuses = [
        call(client, "POST", REFRESH, json={"refresh_token": "x.y.z"}).status_code
        for _ in range(limit + 3)
    ]

    assert statuses[:limit] == [401] * limit, statuses[:limit]
    assert set(statuses[limit:]) == {429}, statuses


def test_each_scope_has_its_own_budget(client):
    """Exhausting OTP requests must not lock a legitimate user out of refresh."""
    for _ in range(settings.otp_request_ip_limit + 2):
        call(client, "POST", OTP_REQUEST, json={"mobile": mobile()})
    assert call(client, "POST", OTP_REQUEST,
                json={"mobile": mobile()}).status_code == 429

    assert call(client, "POST", REFRESH,
                json={"refresh_token": "x.y.z"}).status_code == 401


def test_the_limit_is_a_429_not_a_500(client):
    """Correct semantics: this is an expected client outcome, not a server fault.
    A 500 would also tell a load balancer to page someone."""
    for _ in range(settings.otp_request_ip_limit + 1):
        resp = call(client, "POST", OTP_REQUEST, json={"mobile": mobile()})

    assert resp.status_code == 429
    body = resp.json()
    assert body["title"] == "rate_limited"
    assert body["status"] == 429


def test_the_limiter_fails_closed_when_redis_is_unreachable():
    """The opposite of the membership cache, and deliberately so.

    Failing open here would remove the only thing standing between an attacker
    and unlimited OTP sends — and load is exactly what an attacker creates, so
    'degrade under pressure' means 'switch off when it matters'.
    """

    class DeadRedis:
        def pipeline(self, transaction=True):
            raise ConnectionError("redis is down")

    with pytest.raises(RateLimitedError):
        db.run(
            RateLimiter(DeadRedis()).hit(
                "otp:request", "1.2.3.4", limit=10, window_seconds=600
            )
        )


def test_a_redis_outage_is_indistinguishable_from_being_limited():
    """Whether the limiter is down or the caller is throttled is not the caller's
    business, and the difference would tell an attacker when to push harder."""

    class DeadRedis:
        def pipeline(self, transaction=True):
            raise ConnectionError("redis is down")

    async def _both():
        limiter = RateLimiter(get_redis())
        for _ in range(11):
            try:
                await limiter.hit("probe", "x", limit=10, window_seconds=60)
            except RateLimitedError as exc:
                throttled = exc
                break
        else:  # pragma: no cover
            raise AssertionError("limiter never fired")
        try:
            await RateLimiter(DeadRedis()).hit("probe", "x", limit=10, window_seconds=60)
        except RateLimitedError as exc:
            return throttled, exc
        raise AssertionError("dead redis did not deny")  # pragma: no cover

    throttled, outage = db.run(_both())
    assert type(throttled) is type(outage)
    assert throttled.http_status == outage.http_status == 429


def test_the_client_identity_ignores_a_spoofable_header():
    """`X-Forwarded-For` is caller-supplied. Keying the limiter on it would hand
    an attacker a free budget reset on every request — vary the header, get a new
    bucket, and the limit becomes decorative."""

    class Req:
        headers = {"X-Forwarded-For": "9.9.9.9", "X-Real-IP": "8.8.8.8"}

        class client:
            host = "127.0.0.1"

    assert client_identity(Req()) == "127.0.0.1"


def test_the_per_mobile_cooldown_still_applies(client):
    """The pre-existing control is complementary, not replaced: this one bounds
    messages to a single number, the new one bounds how many numbers."""
    target = mobile()

    first = call(client, "POST", OTP_REQUEST, json={"mobile": target})
    second = call(client, "POST", OTP_REQUEST, json={"mobile": target})

    assert first.status_code == 201, first.text
    assert second.status_code == 429, second.text
