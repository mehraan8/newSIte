"""Offer ownership end-to-end against real PostgreSQL (Phase 1G).

The unit suite proves the policy with a Principal handed to it. This one proves
the Principal itself — assembled by the real `SqlMembershipQuery` from real
`sellers` and `seller_members` rows — produces the same answers, and that a
denial leaves the database exactly as it was.

That second half is the part only a real database can show. A 403 whose
transaction still committed would pass every in-memory assertion: the offer
object would be reverted in Python and the row would be wrong on disk. Here the
row is read back.

Real: PostgreSQL, the Alembic schema, the SQL membership query, the SQLAlchemy
UoW, the outbox writer. Nothing about ownership is faked.
"""

from __future__ import annotations

import uuid

import pytest

from src.api.security import PrincipalFactory
from src.modules.identity.application.ports import AccessTokenClaims
from src.modules.merchant.domain.entities import SellerMemberRole as R
from src.modules.merchant.infrastructure.membership_query import SqlMembershipQuery
from src.modules.selling.application.dtos import (
    ChangeOfferPriceCommand,
    CreateOfferCommand,
)
from src.modules.selling.application.use_cases import (
    ActivateOffer,
    ChangeOfferPrice,
    CreateOffer,
    SuspendOffer,
)
from src.modules.selling.infrastructure.unit_of_work import SqlAlchemySellingUnitOfWork
from src.shared_kernel.application.errors import ForbiddenError
from src.shared_kernel.application.event_context import system_event_context
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
        db.truncate()  # offers, offer_prices, outbox_messages, ...
        db.execute("delete from seller_members")
        db.execute("delete from sellers")

    _wipe()
    yield
    _wipe()


# --- fixtures ---------------------------------------------------------------
def make_seller(status: str = "active") -> uuid.UUID:
    seller_id = uuid.uuid4()
    db.execute(
        "insert into sellers (id, code, name, status, version) "
        "values (:i, :c, 'فروشگاه', :s, 1)",
        i=seller_id, c=f"s-{uuid.uuid4().hex[:10]}", s=status,
    )
    return seller_id


def add_member(seller_id, user_id, role=R.OWNER, status: str = "active") -> None:
    db.execute(
        "insert into seller_members (seller_id, user_id, member_role_code, status) "
        "values (:s, :u, :r, :st)",
        s=seller_id, u=user_id, r=role.value, st=status,
    )


def act(coro):
    """Run a use case the way the app runs one: inside an EventContext.

    The outbox writer refuses to publish without one, so a successful commit
    needs it. Denials are wrapped too — a policy that only worked outside a
    request context would be a bug this suite should not hide.
    """
    with system_event_context():
        return db.run(coro)


def principal_for(user_id: uuid.UUID):
    """Assembled by the REAL membership adapter from the REAL rows.

    Platform grants are stubbed empty on purpose: seller authority must come from
    the membership alone, so a test that accidentally depended on a platform
    permission would fail rather than pass for the wrong reason.
    """

    async def _go():
        async with db.session_factory()() as session:
            class NoGrants:
                async def permission_codes_for_roles(self, role_codes):
                    return frozenset()

            factory = PrincipalFactory(NoGrants(), memberships=SqlMembershipQuery(session))
            return await factory.from_claims(AccessTokenClaims(user_id, None, ()))

    return db.run(_go())


def seed_variant() -> uuid.UUID:
    """A real catalog variant, so Selling's ACL passes for real rather than
    short-circuiting into a 404 that could hide a missing check."""

    async def _go():
        import sqlalchemy as sa

        async with db.engine().begin() as conn:
            ptid = (await conn.execute(sa.text(
                "insert into product_types (code, name_fa, is_active) "
                "values (:c, 'نوع', true) returning id"
            ), {"c": f"pt-{uuid.uuid4().hex[:8]}"})).scalar()
            catid = (await conn.execute(sa.text(
                "insert into categories (slug, name_fa, path, depth, sort_order, is_active) "
                "values (:s, 'دسته', :s, 0, 0, true) returning id"
            ), {"s": f"cat-{uuid.uuid4().hex[:8]}"})).scalar()
            pid, vid = uuid.uuid4(), uuid.uuid4()
            await conn.execute(sa.text(
                "insert into products (id, product_type_id, category_id, slug, name_fa, status) "
                "values (:id, :pt, :cat, :slug, 'کالا', 'active')"
            ), {"id": pid, "pt": ptid, "cat": catid, "slug": f"p-{uuid.uuid4().hex[:8]}"})
            await conn.execute(sa.text(
                "insert into product_variants (id, product_id, sku, variant_axis_values, is_active) "
                "values (:id, :p, :sku, '{}', true)"
            ), {"id": vid, "p": pid, "sku": f"SKU-{uuid.uuid4().hex[:8]}"})
            return vid

    variant_id = db.run(_go())
    CATALOG.known.add(variant_id)
    return variant_id


class Catalog:
    """The catalog ACL, answering from the variants this test actually seeded.

    Not a query: `variant_exists` is awaited inside the use case, on the suite's
    single event loop, and re-entering that loop through the sync `db.*` helpers
    deadlocks. The real gateway is covered by tests/integration/test_ordering_pricing_acl.py;
    what matters here is that a real, existing variant does not mask a missing
    authorization check behind a 404.
    """

    def __init__(self) -> None:
        self.known: set[uuid.UUID] = set()

    async def variant_exists(self, variant_id):
        return variant_id in self.known


CATALOG = Catalog()


def use_cases():
    uow = SqlAlchemySellingUnitOfWork
    return (
        CreateOffer(uow_factory=uow, catalog=CATALOG),
        ActivateOffer(uow_factory=uow, one_active_per_variant=True),
        SuspendOffer(uow_factory=uow),
        ChangeOfferPrice(uow_factory=uow),
    )


def seed_offer(seller_id, variant_id, *, status="draft", amount=50_000) -> uuid.UUID:
    offer_id = uuid.uuid4()
    db.execute(
        "insert into offers (id, seller_id, variant_id, condition, currency, status) "
        "values (:i, :s, :v, 'new', 'IRR', :st)",
        i=offer_id, s=seller_id, v=variant_id, st=status,
    )
    db.execute(
        "insert into offer_prices (offer_id, amount, currency, scale) "
        "values (:o, :a, 'IRR', 0)",
        o=offer_id, a=amount,
    )
    return offer_id


def offer_row(offer_id):
    return db.rows("select * from offers where id = :i", i=offer_id)[0]


def outbox_types() -> list[str]:
    return [r["event_type"] for r in db.rows("select event_type from outbox_messages")]


# ===========================================================================
# create — the seller comes from a real membership row
# ===========================================================================
def test_a_real_member_creates_an_offer_owned_by_their_seller():
    user, seller, variant = uuid.uuid4(), make_seller(), seed_variant()
    add_member(seller, user, R.OWNER)
    create, *_ = use_cases()

    view = act(create.execute(
        CreateOfferCommand(seller_id=str(seller), variant_id=str(variant), amount=1000),
        principal_for(user),
    ))

    assert view.seller_id == str(seller)
    assert offer_row(uuid.UUID(view.id))["seller_id"] == seller
    assert "selling.OfferCreated" in outbox_types()


def test_creating_for_a_seller_you_are_not_a_member_of_writes_nothing():
    """The IDOR, at the storage layer: no offer row and no outbox row."""
    user, mine, victim = uuid.uuid4(), make_seller(), make_seller()
    add_member(mine, user, R.OWNER)
    variant = seed_variant()
    create, *_ = use_cases()

    with pytest.raises(ForbiddenError):
        act(create.execute(
            CreateOfferCommand(seller_id=str(victim), variant_id=str(variant), amount=1000),
            principal_for(user),
        ))

    assert db.scalar("select count(*) from offers") == 0
    assert db.scalar("select count(*) from outbox_messages") == 0


def test_ones_own_user_id_is_not_a_seller_id():
    """The original defect, expressed as a request. If `User == Seller` were
    still true anywhere, this would create a row."""
    user, seller, variant = uuid.uuid4(), make_seller(), seed_variant()
    add_member(seller, user, R.OWNER)
    create, *_ = use_cases()

    with pytest.raises(ForbiddenError):
        act(create.execute(
            CreateOfferCommand(seller_id=str(user), variant_id=str(variant), amount=1000),
            principal_for(user),
        ))

    assert db.scalar("select count(*) from offers") == 0


@pytest.mark.parametrize(
    ("seller_status", "member_status"),
    [("suspended", "active"), ("pending", "active"), ("active", "suspended")],
)
def test_inactive_seller_or_membership_creates_nothing(seller_status, member_status):
    """These are filtered in SQL, not in Python — the membership never reaches
    the Principal at all, so the policy denies on an absent grant."""
    user, seller = uuid.uuid4(), make_seller(status=seller_status)
    add_member(seller, user, R.OWNER, status=member_status)
    variant = seed_variant()
    create, *_ = use_cases()

    with pytest.raises(ForbiddenError):
        act(create.execute(
            CreateOfferCommand(seller_id=str(seller), variant_id=str(variant), amount=1000),
            principal_for(user),
        ))

    assert db.scalar("select count(*) from offers") == 0


# ===========================================================================
# existing offers — a denial must leave the row untouched
# ===========================================================================
def test_another_sellers_member_cannot_activate_and_the_row_is_unchanged():
    owner_seller, attacker_seller = make_seller(), make_seller()
    attacker = uuid.uuid4()
    add_member(attacker_seller, attacker, R.OWNER)
    variant = seed_variant()
    offer = seed_offer(owner_seller, variant, status="draft")
    before = dict(offer_row(offer))
    _c, activate, *_ = use_cases()

    with pytest.raises(ForbiddenError):
        act(activate.execute(offer, principal_for(attacker)))

    assert dict(offer_row(offer)) == before
    assert outbox_types() == []


def test_another_sellers_member_cannot_reprice_and_the_price_is_unchanged():
    """The most damaging case: repricing a competitor's goods to 1."""
    owner_seller, attacker_seller = make_seller(), make_seller()
    attacker = uuid.uuid4()
    add_member(attacker_seller, attacker, R.OWNER)
    variant = seed_variant()
    offer = seed_offer(owner_seller, variant, status="active", amount=50_000)
    *_, reprice = use_cases()

    with pytest.raises(ForbiddenError):
        act(reprice.execute(
            offer, ChangeOfferPriceCommand(amount=1, currency="IRR", scale=0),
            principal_for(attacker),
        ))

    assert db.scalar(
        "select count(*) from offer_prices where offer_id = :o and amount = 1", o=offer
    ) == 0
    assert outbox_types() == []


def test_a_staff_member_of_the_owning_seller_cannot_reprice():
    """In-seller role boundary (approval decision A), proved against real rows:
    the membership exists and is active, and it still is not enough."""
    seller, user = make_seller(), uuid.uuid4()
    add_member(seller, user, R.STAFF)
    variant = seed_variant()
    offer = seed_offer(seller, variant, status="active", amount=50_000)
    *_, reprice = use_cases()

    with pytest.raises(ForbiddenError):
        act(reprice.execute(
            offer, ChangeOfferPriceCommand(amount=1, currency="IRR", scale=0),
            principal_for(user),
        ))

    assert db.scalar(
        "select count(*) from offer_prices where offer_id = :o and amount = 1", o=offer
    ) == 0


def test_the_owning_sellers_manager_completes_the_lifecycle():
    """The counterweight: the check must not lock out the legitimate member."""
    seller, user = make_seller(), uuid.uuid4()
    add_member(seller, user, R.MANAGER)
    variant = seed_variant()
    offer = seed_offer(seller, variant, status="draft", amount=50_000)
    _c, activate, suspend, reprice = use_cases()
    principal = principal_for(user)

    assert act(activate.execute(offer, principal)).status == "active"
    assert act(reprice.execute(
        offer, ChangeOfferPriceCommand(amount=42_000, currency="IRR", scale=0), principal
    )).price.amount == 42_000
    assert act(suspend.execute(offer, principal)).status == "suspended"

    assert offer_row(offer)["status"] == "suspended"
    assert "selling.OfferActivated" in outbox_types()


def test_a_colleague_in_the_same_seller_can_act_on_the_offer():
    """Ownership is the seller's, not the creating user's."""
    seller, creator, colleague = make_seller(), uuid.uuid4(), uuid.uuid4()
    add_member(seller, creator, R.MANAGER)
    add_member(seller, colleague, R.MANAGER)
    variant = seed_variant()
    create, activate, *_ = use_cases()

    view = act(create.execute(
        CreateOfferCommand(seller_id=str(seller), variant_id=str(variant), amount=1000),
        principal_for(creator),
    ))
    activated = act(activate.execute(uuid.UUID(view.id), principal_for(colleague)))

    assert activated.status == "active"


def test_multi_membership_resolves_per_seller_from_real_rows():
    """One user, staff in A and manager in B — both rows real, both answers
    different."""
    user = uuid.uuid4()
    seller_a, seller_b = make_seller(), make_seller()
    add_member(seller_a, user, R.STAFF)
    add_member(seller_b, user, R.MANAGER)
    variant = seed_variant()
    offer_a = seed_offer(seller_a, variant, status="draft")
    offer_b = seed_offer(seller_b, variant, status="draft")
    _c, activate, *_ = use_cases()
    principal = principal_for(user)

    with pytest.raises(ForbiddenError):
        act(activate.execute(offer_a, principal))
    assert act(activate.execute(offer_b, principal)).status == "active"
    assert offer_row(offer_a)["status"] == "draft"


def test_revoking_a_membership_removes_authority():
    """No token is reissued: authority is resolved per request from the rows, so
    deleting the grant stops the next call."""
    seller, user = make_seller(), uuid.uuid4()
    add_member(seller, user, R.MANAGER)
    variant = seed_variant()
    offer = seed_offer(seller, variant, status="draft")
    _c, activate, suspend, _r = use_cases()

    assert act(activate.execute(offer, principal_for(user))).status == "active"

    db.execute("delete from seller_members where user_id = :u", u=user)

    with pytest.raises(ForbiddenError):
        act(suspend.execute(offer, principal_for(user)))
    assert offer_row(offer)["status"] == "active"
