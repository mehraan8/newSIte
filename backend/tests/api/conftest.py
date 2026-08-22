"""HTTP-layer test harness.

Why this suite exists: before it, 249 tests passed and none of them touched
FastAPI. Routers, dependency wiring, `get_current_user`, `require_roles` and the
error handlers had zero coverage — which is exactly how ten write endpoints
shipped with no authentication at all. Domain tests cannot catch a missing
`Depends`; only a request can.

The app is driven in-process over ASGITransport: no live server, no port, no
network. It runs against the same scratch database as tests/integration and
deliberately reuses that suite's engine, event loop and Alembic bootstrap rather
than standing up a second harness that could drift from it.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from tests.integration import db

db.skip_if_unavailable()  # module-level: an unrun suite must never look green

from src.api.main import app  # noqa: E402
from src.modules.identity.application.roles import PlatformRole  # noqa: E402
from src.modules.identity.infrastructure.jwt_service import JwtTokenService  # noqa: E402
from src.modules.merchant.domain.entities import SellerMemberRole  # noqa: E402
from src.modules.merchant.infrastructure.membership_cache import (  # noqa: E402
    MEMBERSHIP_KEY,
    SELLER_KEY,
)
from src.shared_kernel.infrastructure.redis_client import get_redis  # noqa: E402

pytestmark = pytest.mark.integration

# Everything the API suite writes. Broader than tests/integration's TABLES
# because these tests exercise Identity and Catalog, which that suite does not.
# Truncated as one CASCADE statement so FK order does not matter.
API_TABLES = (
    "order_idempotency",
    "order_saga_lines",
    "orders_saga",
    "inventory_reservation",
    "inventory_stock",
    "processed_events",
    "dead_letter_messages",
    "outbox_messages",
    "buy_box_variant_state",
    "buy_box_offer",
    "notifications",
    "seller_members",
    "sellers",
    "offer_prices",
    "offers",
    "prices",
    "product_variants",
    "products",
    "categories",
    "brands",
    "product_types",
    "user_roles",
    "refresh_tokens",
    "users",
)


@pytest.fixture(scope="session", autouse=True)
def schema():
    ok, _reason, _dsn = db.availability()
    if not ok:  # pragma: no cover - guarded by skip_if_unavailable
        yield
        return
    db.migrate()
    yield


@pytest.fixture(autouse=True)
def clean(schema):
    """Every test starts from an empty database — no ordering coupling."""

    async def _truncate():
        async with db.engine().begin() as conn:
            existing = set(
                (
                    await conn.execute(
                        sa.text(
                            "select table_name from information_schema.tables "
                            "where table_schema='public'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            targets = [t for t in API_TABLES if t in existing]
            if targets:
                await conn.execute(
                    sa.text(f"truncate {', '.join(targets)} restart identity cascade")
                )

    db.run(_truncate())
    yield


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Clear the shared rate-limit counters between tests.

    Every test in this suite arrives from the same client address, so without
    this the whole file shares one budget and tests fail depending on how many
    ran before them — order-dependent, and the failure looks like a broken
    endpoint rather than a full bucket.

    This isolates tests; it does NOT disable the control. The limiter runs
    normally inside each test, and tests/api/test_rate_limiting.py asserts that
    it fires, that it fails closed, and that the budgets are per scope.
    """

    async def _clear():
        redis = get_redis()
        keys = [k async for k in redis.scan_iter(match="ratelimit:*", count=500)]
        if keys:
            await redis.delete(*keys)

    db.run(_clear())
    yield


@pytest.fixture
def client():
    """AsyncClient bound to the real app, reusable for many calls per test.

    `raise_app_exceptions=False` so an unhandled handler error surfaces as the 500
    a real caller would receive rather than propagating into the test — that is
    precisely what the error-mapping assertions inspect.
    """
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )
    yield c
    db.run(c.aclose())


def call(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    """Synchronous request on the suite's shared event loop.

    The suite is sync-style (like tests/integration) because asyncpg connections
    are loop-bound and a fresh asyncio.run() per test would poison the pool.
    """
    return db.run(client.request(method, url, **kw))


# --- factories -------------------------------------------------------------
def make_user(*, status: str = "active", roles: tuple[str, ...] = ()) -> uuid.UUID:
    """Insert a user and its PLATFORM role grants directly.

    Bypasses the OTP flow on purpose: the authorization matrix needs dozens of
    principals and should not depend on Redis or on OTP behaviour. The identity
    lifecycle tests use the real flow instead.

    `roles` are platform roles only. Seller authority is NOT a role — it is a
    membership, granted with `add_member` below.
    """
    user_id = uuid.uuid4()
    mobile = "+989" + str(uuid.uuid4().int)[:9]
    db.execute(
        "insert into users (id, mobile, status, is_phone_verified) "
        "values (:id, :m, :s, true)",
        id=user_id, m=mobile, s=status,
    )
    for role in roles:
        # Grant via role code, resolving to the seeded roles row — same path the
        # application uses, so a missing seed fails the test rather than hiding.
        role_id = db.scalar("select id from roles where code = :c", c=role)
        assert role_id is not None, (
            f"role {role!r} is not seeded; migration 0020 should have created it"
        )
        db.execute(
            "insert into user_roles (user_id, role_id) values (:u, :r) "
            "on conflict do nothing",
            u=user_id, r=role_id,
        )
    return user_id


def access_token(user_id: uuid.UUID, roles: tuple[str, ...] = ()) -> str:
    """A genuinely signed access token, with iss/aud, via the real service."""
    return JwtTokenService().create_access_token(user_id, list(roles))


def auth(user_id: uuid.UUID, roles: tuple[str, ...] = ()) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token(user_id, roles)}"}


def refresh_session(user_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    """A real refresh-token family for `user_id`. Returns (family_id, raw token).

    Minted by the real `JwtTokenService` and persisted in `refresh_tokens` the
    way `VerifyOtp._issue_tokens` does, so rotation, reuse detection and family
    revocation all see exactly what they would in production. Bypasses the OTP
    flow only to avoid depending on Redis and OTP timing.
    """
    family_id = uuid.uuid4()
    raw, token_hash = JwtTokenService().create_refresh_token(user_id, family_id)
    db.execute(
        "insert into refresh_tokens (id, user_id, token_hash, family_id, expires_at) "
        "values (:i, :u, :h, :f, now() + interval '30 days')",
        i=uuid.uuid4(), u=user_id, h=token_hash, f=family_id,
    )
    return family_id, raw


def live_tokens(family_id: uuid.UUID) -> int:
    """Unrevoked rows in a family — what revocation is actually measured by."""
    return db.scalar(
        "select count(*) from refresh_tokens "
        "where family_id = :f and revoked_at is null",
        f=family_id,
    )


def principal(*roles: str, status: str = "active") -> tuple[uuid.UUID, dict[str, str]]:
    """A user with `roles` granted in the database, plus ready-made headers."""
    uid = make_user(status=status, roles=roles)
    return uid, auth(uid, roles)


def insert_returning(sql: str, **params):
    """INSERT ... RETURNING inside a committed transaction.

    `db.scalar` and `db.rows` open a plain `connect()`, which SQLAlchemy rolls
    back on exit — fine for reads, silently discards writes. Anything that must
    persist has to go through `begin()`.
    """

    async def _go():
        async with db.engine().begin() as conn:
            return (await conn.execute(sa.text(sql), params)).scalar()

    return db.run(_go())


def seed_variant() -> uuid.UUID:
    """A real catalog variant, so Selling's ACL check passes for real."""
    ptid = insert_returning(
        "insert into product_types (code, name_fa, is_active) "
        "values (:c, 'نوع', true) returning id",
        c=f"pt-{uuid.uuid4().hex[:8]}",
    )
    catid = insert_returning(
        "insert into categories (slug, name_fa, path, depth, sort_order, is_active) "
        "values (:s, 'دسته', :s, 0, 0, true) returning id",
        s=f"cat-{uuid.uuid4().hex[:8]}",
    )
    pid = uuid.uuid4()
    db.execute(
        "insert into products (id, product_type_id, category_id, slug, name_fa, status) "
        "values (:id, :pt, :cat, :slug, 'کالا', 'active')",
        id=pid, pt=ptid, cat=catid, slug=f"p-{uuid.uuid4().hex[:8]}",
    )
    vid = uuid.uuid4()
    db.execute(
        "insert into product_variants (id, product_id, sku, variant_axis_values, is_active) "
        "values (:id, :p, :sku, '{}', true)",
        id=vid, p=pid, sku=f"SKU-{uuid.uuid4().hex[:8]}",
    )
    return vid


def seed_offer(
    seller_id: uuid.UUID, variant_id: uuid.UUID, *, status: str = "active",
    amount: int | None = None,
) -> uuid.UUID:
    """An offer owned by a SELLER — never by a user (ADR-0011 §7.1)."""
    offer_id = uuid.uuid4()
    db.execute(
        "insert into offers (id, seller_id, variant_id, condition, currency, status) "
        "values (:id, :s, :v, 'new', 'IRR', :st)",
        id=offer_id, s=seller_id, v=variant_id, st=status,
    )
    if amount is not None:
        db.execute(
            "insert into offer_prices (offer_id, amount, currency, scale) "
            "values (:o, :a, 'IRR', 0)",
            o=offer_id, a=amount,
        )
    return offer_id


# --- merchant: sellers and memberships -------------------------------------
# A Seller is a business, and a User reaches it only through a SellerMember row.
# These two factories are the ONLY way this suite confers seller authority —
# there is no role to grant and no token claim to set.
def make_seller(*, status: str = "active") -> uuid.UUID:
    seller_id = uuid.uuid4()
    db.execute(
        "insert into sellers (id, code, name, status, version) "
        "values (:i, :c, 'فروشگاه', :s, 1)",
        i=seller_id, c=f"s-{uuid.uuid4().hex[:10]}", s=status,
    )
    return seller_id


def add_member(
    seller_id: uuid.UUID, user_id: uuid.UUID,
    role: SellerMemberRole = SellerMemberRole.OWNER, *, status: str = "active",
) -> None:
    db.execute(
        "insert into seller_members (seller_id, user_id, member_role_code, status) "
        "values (:s, :u, :r, :st)",
        s=seller_id, u=user_id, r=role.value, st=status,
    )
    forget(user_id, seller_id)


def forget(user_id: uuid.UUID, *seller_ids: uuid.UUID) -> None:
    """Drop the authorization cache entries for this user and these sellers.

    Membership rows are inserted with raw SQL here, so the invalidation the
    application would perform on a real membership change never happens. Without
    this, a request made before the insert would leave a negative cache entry
    that outlives the test. Tests that assert on REVOCATION call it explicitly,
    which is also what the production write path must do.
    """
    async def _go():
        redis = get_redis()
        keys = [MEMBERSHIP_KEY.format(user_id=user_id)]
        keys += [SELLER_KEY.format(seller_id=s) for s in seller_ids]
        await redis.delete(*keys)

    db.run(_go())


def member(
    role: SellerMemberRole = SellerMemberRole.OWNER, *,
    seller_id: uuid.UUID | None = None, seller_status: str = "active",
    member_status: str = "active",
) -> tuple[uuid.UUID, uuid.UUID, dict[str, str]]:
    """A user who is a member of a seller. Returns (seller_id, user_id, headers)."""
    seller = seller_id if seller_id is not None else make_seller(status=seller_status)
    user = make_user()
    add_member(seller, user, role, status=member_status)
    return seller, user, auth(user)


__all__ = [
    "PlatformRole",
    "SellerMemberRole",
    "access_token",
    "add_member",
    "auth",
    "call",
    "forget",
    "insert_returning",
    "live_tokens",
    "refresh_session",
    "make_seller",
    "make_user",
    "member",
    "principal",
    "seed_offer",
    "seed_variant",
]
