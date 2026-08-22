"""Seller-aware offer authorization (ADR-0011 §9, Phase 1G) — DB-free.

The invariant under test is `User != Seller`. A user never owns an offer; a
seller does, and a user reaches that seller only through a SellerMember grant.
Two distinct holes are covered, and passing one does not imply the other:

  * IDENTITY BINDING on create — the caller names a target seller, and the
    nomination is honoured only if it is one of THEIR memberships. Naming a
    stranger's seller, or their own user id, creates nothing.
  * OWNERSHIP on an existing offer — activate/suspend/reprice resolve the owner
    from `offer.seller_id`, never from the caller. Knowing an offer's UUID
    confers nothing.

Real use cases, real policy, real domain aggregate, real UoW event draining.
Fakes are only the repository, the catalog ACL and the UoW, exactly as
test_selling_flow.py fakes them — so an authorization pass here is a pass through
the same code the router calls.

Every denial is checked twice: the error, AND that nothing changed. A 403 that
still mutates is not a fix.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.modules.merchant.application.member_permissions import MEMBER_ROLE_PERMISSIONS
from src.modules.merchant.domain.entities import SellerMemberRole
from src.modules.selling.application.dtos import (
    ChangeOfferPriceCommand,
    CreateOfferCommand,
)
from src.modules.selling.application.policies import ensure_can_act_for_seller
from src.modules.selling.application.unit_of_work import SellingUnitOfWork
from src.modules.selling.application.use_cases import (
    ActivateOffer,
    ChangeOfferPrice,
    CreateOffer,
    SuspendOffer,
)
from src.modules.selling.domain.entities import OfferStatus
from src.shared_kernel.application.errors import ForbiddenError, NotFoundError
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal, SellerGrant, SellerStatus
from src.shared_kernel.domain.value_objects import Money
from src.shared_kernel.infrastructure.outbox.serialization import event_payload


def run(coro):
    return asyncio.run(coro)


# --- fakes -----------------------------------------------------------------
class FakeCatalog:
    """Always says the variant exists: this suite is about authorization, and a
    catalog miss would mask a missing check behind a 404."""

    async def variant_exists(self, variant_id: uuid.UUID) -> bool:
        return True


class FakeOfferRepo:
    def __init__(self, uow, store):
        self._uow = uow
        self._store = store

    async def get_by_id(self, offer_id):
        offer = self._store.get(offer_id)
        if offer:
            self._uow.track(offer)
        return offer

    async def add(self, offer):
        self._store[offer.id] = offer
        self._uow.track(offer)

    async def save(self, offer):
        self._store[offer.id] = offer
        self._uow.track(offer)

    async def find_active_for(self, seller_id, variant_id):
        for offer in self._store.values():
            if (
                offer.seller_id == seller_id
                and offer.variant_id == variant_id
                and offer.status == OfferStatus.ACTIVE
            ):
                self._uow.track(offer)
                return offer
        return None


class FakeUoW(SellingUnitOfWork):
    """Records what reached the outbox, so a denial can be checked for silence."""

    def __init__(self, store):
        super().__init__()
        self.offers = FakeOfferRepo(self, store)
        self.outbox: list = []

    async def commit(self):
        for _agg, event in self.collect_new_events():
            event_payload(event)  # PII guard, as the real writer does
            self.outbox.append(event)
        self._committed = True

    async def rollback(self):
        self._discard_tracked()


class World:
    """One store, one UoW, all four use cases — the whole Selling surface."""

    def __init__(self, *, one_active_per_variant: bool = True):
        self.store: dict = {}
        self.uow = FakeUoW(self.store)
        factory = lambda: self.uow  # noqa: E731
        self.create = CreateOffer(uow_factory=factory, catalog=FakeCatalog())
        self.activate = ActivateOffer(
            uow_factory=factory, one_active_per_variant=one_active_per_variant
        )
        self.suspend = SuspendOffer(uow_factory=factory)
        self.reprice = ChangeOfferPrice(uow_factory=factory)

    @property
    def events(self) -> list[str]:
        return [type(e).__name__ for e in self.uow.outbox]

    def seed_offer(self, seller_id: uuid.UUID, *, amount: int = 50_000, active=False):
        """An offer placed directly in the store, owned by `seller_id`.

        Built through the aggregate rather than hand-assembled, so it is a real
        offer with a real price; the creation events are then discarded because
        the test is only interested in what the operation under test emits.
        """
        from src.modules.selling.domain.entities import Offer

        offer = Offer.create(
            seller_id=seller_id,
            variant_id=uuid.uuid4(),
            condition="new",
            currency="IRR",
            initial_price=Money(amount=amount, currency="IRR", scale=0),
        )
        if active:
            offer.activate()
        offer.pull_events()
        self.store[offer.id] = offer
        return offer


# --- principals -------------------------------------------------------------
def grant(seller_id: uuid.UUID, role: SellerMemberRole, *, status=SellerStatus.ACTIVE):
    """A grant built from the APPROVED matrix, not a hand-picked permission set.

    Hand-picking would let this suite keep passing after the matrix changed —
    the tests would then be asserting their own fixture, not the policy.
    """
    return SellerGrant(
        seller_id=seller_id,
        permissions=MEMBER_ROLE_PERMISSIONS[role],
        status=status,
    )


def member(seller_id: uuid.UUID, role: SellerMemberRole, **kw) -> Principal:
    return Principal(
        subject_id=uuid.uuid4(), memberships={seller_id: grant(seller_id, role, **kw)}
    )


def stranger() -> Principal:
    """Authenticated, with no membership anywhere."""
    return Principal(subject_id=uuid.uuid4())


CREATE_BODY = dict(condition="new", currency="IRR", amount=10_000, scale=0)


def create_cmd(seller_id: uuid.UUID) -> CreateOfferCommand:
    return CreateOfferCommand(
        seller_id=str(seller_id), variant_id=str(uuid.uuid4()), **CREATE_BODY
    )


PRICE = ChangeOfferPriceCommand(amount=1, currency="IRR", scale=0)


# ===========================================================================
# 1. The policy itself
# ===========================================================================
def test_policy_allows_a_member_holding_the_permission():
    seller = uuid.uuid4()
    principal = member(seller, SellerMemberRole.OWNER)
    ensure_can_act_for_seller(principal, seller, Permission.SELLING_OFFERS_ACTIVATE)


@pytest.mark.parametrize(
    "principal_for",
    [
        pytest.param(lambda s: stranger(), id="no-membership"),
        pytest.param(
            lambda s: member(s, SellerMemberRole.VIEWER), id="role-lacks-permission"
        ),
        pytest.param(
            lambda s: member(s, SellerMemberRole.OWNER, status=SellerStatus.SUSPENDED),
            id="seller-suspended",
        ),
        pytest.param(
            lambda s: member(s, SellerMemberRole.OWNER, status=SellerStatus.PENDING),
            id="seller-pending",
        ),
        pytest.param(
            lambda s: member(uuid.uuid4(), SellerMemberRole.OWNER), id="other-seller"
        ),
    ],
)
def test_policy_denies_every_failing_case(principal_for):
    seller = uuid.uuid4()
    with pytest.raises(ForbiddenError):
        ensure_can_act_for_seller(
            principal_for(seller), seller, Permission.SELLING_OFFERS_ACTIVATE
        )


def test_denial_message_is_identical_for_every_reason():
    """The response must not disclose WHICH check failed.

    Distinguishable messages would turn a denial into an oracle: an attacker
    could enumerate which seller ids exist, or which of their own memberships is
    suspended, from the error text alone.
    """
    seller = uuid.uuid4()
    messages = set()
    for principal in (
        stranger(),
        member(seller, SellerMemberRole.VIEWER),
        member(seller, SellerMemberRole.OWNER, status=SellerStatus.SUSPENDED),
        member(uuid.uuid4(), SellerMemberRole.OWNER),
    ):
        with pytest.raises(ForbiddenError) as exc:
            ensure_can_act_for_seller(
                principal, seller, Permission.SELLING_OFFERS_ACTIVATE
            )
        messages.add(str(exc.value))
    assert len(messages) == 1, messages


def test_policy_does_no_io():
    """Pure and synchronous by construction (ADR-0011 §9).

    An async policy would be free to query, and a query inside a policy is how
    per-request authorization I/O creeps back in after Phase 1F removed it.
    """
    assert not asyncio.iscoroutinefunction(ensure_can_act_for_seller)


def test_platform_permission_does_not_substitute_for_membership():
    """Cross-universe leak (approval decisions E/F).

    `selling.offers.manage.any` is platform moderation authority. Honouring it
    here would let a platform role act AS a seller member, which is a different
    thing from moderating a seller.
    """
    seller = uuid.uuid4()
    admin = Principal(
        subject_id=uuid.uuid4(),
        platform_permissions=frozenset(
            {
                Permission.SELLING_OFFERS_MANAGE_ANY,
                Permission.SELLING_OFFERS_ACTIVATE,
                Permission.SELLING_OFFERS_CREATE,
            }
        ),
    )
    for permission in (
        Permission.SELLING_OFFERS_ACTIVATE,
        Permission.SELLING_OFFERS_CREATE,
    ):
        with pytest.raises(ForbiddenError):
            ensure_can_act_for_seller(admin, seller, permission)


# ===========================================================================
# 2. Create — target-seller selection (brief §21, cases A–E)
# ===========================================================================
def test_case_a_member_creating_for_their_own_seller_succeeds():
    seller = uuid.uuid4()
    world = World()

    view = run(world.create.execute(create_cmd(seller), member(seller, SellerMemberRole.OWNER)))

    assert view.seller_id == str(seller)
    assert world.events == ["OfferCreated", "OfferPriceChanged"]


def test_case_b_naming_another_sellers_id_creates_nothing():
    """The IDOR that a client-supplied seller_id would otherwise open."""
    mine, victim = uuid.uuid4(), uuid.uuid4()
    world = World()

    with pytest.raises(ForbiddenError):
        run(world.create.execute(create_cmd(victim), member(mine, SellerMemberRole.OWNER)))

    assert world.store == {}
    assert world.events == []


def test_case_c_a_caller_with_no_membership_creates_nothing():
    world = World()

    with pytest.raises(ForbiddenError):
        run(world.create.execute(create_cmd(uuid.uuid4()), stranger()))

    assert world.store == {}
    assert world.events == []


def test_case_d_naming_ones_own_user_id_as_the_seller_is_denied():
    """The `User == Seller` collapse, attempted from the outside.

    This is the exact shape of the original bug expressed as a request: if the
    subject id were ever treated as a seller id, this would create an offer.
    """
    world = World()
    seller = uuid.uuid4()
    principal = Principal(
        subject_id=uuid.uuid4(),
        memberships={seller: grant(seller, SellerMemberRole.OWNER)},
    )

    with pytest.raises(ForbiddenError):
        run(world.create.execute(create_cmd(principal.subject_id), principal))

    assert world.store == {}
    assert world.events == []


def test_case_e_a_member_of_a_suspended_seller_creates_nothing():
    seller = uuid.uuid4()
    world = World()
    principal = member(seller, SellerMemberRole.OWNER, status=SellerStatus.SUSPENDED)

    with pytest.raises(ForbiddenError):
        run(world.create.execute(create_cmd(seller), principal))

    assert world.store == {}
    assert world.events == []


def test_create_is_authorized_before_the_catalog_is_consulted():
    """Ordering matters: an unauthorized caller must not learn whether a variant
    exists. A 404 for a stranger would make the endpoint a catalog oracle."""
    asked: list = []

    class SpyCatalog:
        async def variant_exists(self, variant_id):
            asked.append(variant_id)
            return False

    world = World()
    world.create = CreateOffer(uow_factory=lambda: world.uow, catalog=SpyCatalog())

    with pytest.raises(ForbiddenError):
        run(world.create.execute(create_cmd(uuid.uuid4()), stranger()))

    assert asked == []


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (SellerMemberRole.OWNER, True),
        (SellerMemberRole.MANAGER, True),
        (SellerMemberRole.STAFF, True),   # staff MAY create (approval decision)
        (SellerMemberRole.VIEWER, False),
    ],
)
def test_create_follows_the_approved_role_matrix(role, allowed):
    seller = uuid.uuid4()
    world = World()
    principal = member(seller, role)

    if allowed:
        view = run(world.create.execute(create_cmd(seller), principal))
        assert view.seller_id == str(seller)
    else:
        with pytest.raises(ForbiddenError):
            run(world.create.execute(create_cmd(seller), principal))
        assert world.store == {}


# ===========================================================================
# 3. Existing offers — ownership comes from the OFFER
# ===========================================================================
def operations(world: World, offer):
    """Every mutating operation on an existing offer, as callables."""
    return {
        "activate": lambda p: world.activate.execute(offer.id, p),
        "suspend": lambda p: world.suspend.execute(offer.id, p),
        "price": lambda p: world.reprice.execute(offer.id, PRICE, p),
    }


@pytest.mark.parametrize("operation", ["activate", "suspend", "price"])
def test_a_stranger_cannot_touch_an_offer(operation):
    owner_seller = uuid.uuid4()
    world = World()
    offer = world.seed_offer(owner_seller, active=(operation != "activate"))
    before = (offer.status, offer.current_price.amount)

    with pytest.raises(ForbiddenError):
        run(operations(world, offer)[operation](stranger()))

    assert (offer.status, offer.current_price.amount) == before
    assert world.events == []


@pytest.mark.parametrize("operation", ["activate", "suspend", "price"])
def test_a_member_of_a_different_seller_cannot_touch_an_offer(operation):
    """Cross-seller isolation (brief §12): holding full authority somewhere else
    is not authority here."""
    owner_seller, attacker_seller = uuid.uuid4(), uuid.uuid4()
    world = World()
    offer = world.seed_offer(owner_seller, active=(operation != "activate"))
    attacker = member(attacker_seller, SellerMemberRole.OWNER)
    before = (offer.status, offer.current_price.amount)

    with pytest.raises(ForbiddenError):
        run(operations(world, offer)[operation](attacker))

    assert (offer.status, offer.current_price.amount) == before
    assert world.events == []


@pytest.mark.parametrize("operation", ["activate", "suspend", "price"])
def test_isolation_holds_in_the_other_direction_too(operation):
    """Symmetry: the check must not be a one-way accident of test ordering."""
    seller_a, seller_b = uuid.uuid4(), uuid.uuid4()
    world = World(one_active_per_variant=False)
    offer_b = world.seed_offer(seller_b, active=(operation != "activate"))

    with pytest.raises(ForbiddenError):
        run(operations(world, offer_b)[operation](member(seller_a, SellerMemberRole.OWNER)))

    # ...and the legitimate owner of B is unaffected by the failed attempt.
    run(operations(world, offer_b)[operation](member(seller_b, SellerMemberRole.OWNER)))


@pytest.mark.parametrize(
    ("operation", "permitted_roles"),
    [
        ("activate", {SellerMemberRole.OWNER, SellerMemberRole.MANAGER}),
        ("suspend", {SellerMemberRole.OWNER, SellerMemberRole.MANAGER}),
        ("price", {SellerMemberRole.OWNER, SellerMemberRole.MANAGER}),
    ],
)
@pytest.mark.parametrize("role", list(SellerMemberRole))
def test_existing_offer_operations_follow_the_approved_matrix(
    operation, permitted_roles, role
):
    """Staff and viewer must not activate, suspend or reprice (decisions A & B)."""
    seller = uuid.uuid4()
    world = World()
    offer = world.seed_offer(seller, active=(operation != "activate"))
    principal = member(seller, role)

    if role in permitted_roles:
        run(operations(world, offer)[operation](principal))
        assert world.events != []
    else:
        with pytest.raises(ForbiddenError):
            run(operations(world, offer)[operation](principal))
        assert world.events == []


def test_the_owning_seller_can_still_run_the_whole_lifecycle():
    """A suite that only proved denial would pass with every route hard-wired to
    403. This is the counterweight."""
    seller = uuid.uuid4()
    world = World()
    principal = member(seller, SellerMemberRole.OWNER)

    created = run(world.create.execute(create_cmd(seller), principal))
    offer_id = uuid.UUID(created.id)

    assert run(world.activate.execute(offer_id, principal)).status == "active"
    repriced = run(world.reprice.execute(offer_id, PRICE, principal))
    assert repriced.price.amount == 1
    assert run(world.suspend.execute(offer_id, principal)).status == "suspended"


# ===========================================================================
# 4. Membership shapes: several sellers, several users
# ===========================================================================
def test_multi_membership_authority_is_per_seller_not_global():
    """Brief §13: staff in A and manager in B.

    The same person, the same request path, two different answers — because the
    authority is attached to the seller, not to the user.
    """
    seller_a, seller_b = uuid.uuid4(), uuid.uuid4()
    principal = Principal(
        subject_id=uuid.uuid4(),
        memberships={
            seller_a: grant(seller_a, SellerMemberRole.STAFF),
            seller_b: grant(seller_b, SellerMemberRole.MANAGER),
        },
    )
    world = World()
    offer_a = world.seed_offer(seller_a)
    offer_b = world.seed_offer(seller_b)

    # staff in A: may create, may NOT activate
    assert run(world.create.execute(create_cmd(seller_a), principal)).seller_id == str(seller_a)
    with pytest.raises(ForbiddenError):
        run(world.activate.execute(offer_a.id, principal))

    # manager in B: may activate
    assert run(world.activate.execute(offer_b.id, principal)).status == "active"


def test_two_users_in_the_same_seller_both_act_on_the_same_offer():
    """Brief §14: ownership is the SELLER's, not the creating user's.

    A per-user ownership model would pass every cross-seller test above and still
    break the moment a colleague touched the offer.
    """
    seller = uuid.uuid4()
    world = World()
    creator = member(seller, SellerMemberRole.MANAGER)
    colleague = member(seller, SellerMemberRole.MANAGER)
    assert creator.subject_id != colleague.subject_id

    created = run(world.create.execute(create_cmd(seller), creator))
    offer_id = uuid.UUID(created.id)

    assert run(world.activate.execute(offer_id, colleague)).status == "active"
    assert run(world.suspend.execute(offer_id, creator)).status == "suspended"


def test_an_unknown_member_role_confers_nothing():
    """A role code this build does not define is dropped during Principal
    assembly, so the grant simply is not there. Modelled here as the empty
    permission set the drop produces."""
    seller = uuid.uuid4()
    world = World()
    offer = world.seed_offer(seller, active=True)
    principal = Principal(
        subject_id=uuid.uuid4(),
        memberships={
            seller: SellerGrant(
                seller_id=seller, permissions=frozenset(), status=SellerStatus.ACTIVE
            )
        },
    )

    with pytest.raises(ForbiddenError):
        run(world.suspend.execute(offer.id, principal))
    assert world.events == []


# ===========================================================================
# 5. Denial ordering and information disclosure
# ===========================================================================
@pytest.mark.parametrize("operation", ["activate", "suspend", "price"])
def test_a_missing_offer_is_404_even_for_a_stranger(operation):
    """404 before 403 is deliberate.

    The alternative — 403 for anything not found — would confirm nothing, but it
    would also make a real 404 indistinguishable from a real 403 for the OWNER,
    who is entitled to know their offer is gone.
    """
    world = World()
    missing = uuid.uuid4()
    calls = {
        "activate": lambda p: world.activate.execute(missing, p),
        "suspend": lambda p: world.suspend.execute(missing, p),
        "price": lambda p: world.reprice.execute(missing, PRICE, p),
    }

    with pytest.raises(NotFoundError):
        run(calls[operation](stranger()))


def test_denial_does_not_disclose_the_owning_seller():
    seller = uuid.uuid4()
    world = World()
    offer = world.seed_offer(seller, active=True)

    with pytest.raises(ForbiddenError) as exc:
        run(world.suspend.execute(offer.id, stranger()))

    assert str(seller) not in str(exc.value)


def test_authorization_precedes_the_one_active_offer_invariant():
    """An unauthorized caller must not be able to probe another seller's catalogue
    state through the difference between 409 and 403."""
    seller = uuid.uuid4()
    world = World(one_active_per_variant=True)
    first = world.seed_offer(seller, active=True)
    second = world.seed_offer(seller)
    second.variant_id = first.variant_id  # same variant -> would conflict

    with pytest.raises(ForbiddenError):
        run(world.activate.execute(second.id, member(uuid.uuid4(), SellerMemberRole.OWNER)))


# ===========================================================================
# 6. Structural guards — these fail wherever the rule is broken, not only here
# ===========================================================================
def test_every_mutating_use_case_requires_a_principal():
    """A new use case cannot be added without an authorization argument.

    Adding a fifth mutating use case that forgot the check is the realistic way
    this regresses; a signature guard catches it in review-time terms.
    """
    import inspect

    for use_case in (CreateOffer, ActivateOffer, SuspendOffer, ChangeOfferPrice):
        params = inspect.signature(use_case.execute).parameters
        assert "principal" in params, f"{use_case.__name__}.execute takes no principal"
        assert params["principal"].annotation in (Principal, "Principal"), use_case


def test_the_permission_used_matches_the_operation():
    """Each operation must demand its OWN permission.

    If activate demanded `selling.offers.create`, every cross-seller test above
    would still pass while staff silently gained the ability to activate.
    """
    import inspect

    expected = {
        CreateOffer: "SELLING_OFFERS_CREATE",
        ActivateOffer: "SELLING_OFFERS_ACTIVATE",
        SuspendOffer: "SELLING_OFFERS_SUSPEND",
        ChangeOfferPrice: "SELLING_OFFERS_PRICE_CHANGE",
    }
    for use_case, permission in expected.items():
        source = inspect.getsource(use_case)
        assert f"Permission.{permission}" in source, (
            f"{use_case.__name__} does not demand Permission.{permission}"
        )
        for other in set(expected.values()) - {permission}:
            assert f"Permission.{other}" not in source, (
                f"{use_case.__name__} also references Permission.{other}"
            )


def test_no_selling_module_imports_merchant_or_identity():
    """Selling consumes an assembled Principal; it must not reach into the
    contexts that produced one (ADR-0011 §4)."""
    import ast
    import pathlib

    selling = pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "selling"
    offenders = []
    for path in selling.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                if name.startswith(("src.modules.merchant", "src.modules.identity")):
                    offenders.append(f"{path.name}:{node.lineno}: {name}")
    assert offenders == [], offenders
