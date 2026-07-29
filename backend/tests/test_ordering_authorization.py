"""Ordering authorization: the caller controls neither price nor identity.

Two bypasses this pins shut:
  * unit_amount used to come from the request body — buy anything for 1 rial.
  * customer_id used to come from the request body — order as anybody.

Run: pytest tests/test_ordering_authorization.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="ordering interface pulls in the DB layer")
pytest.importorskip("jwt", reason="identity dependency pulls in PyJWT")

import pydantic  # noqa: E402

from src.modules.ordering.application.dtos import (  # noqa: E402
    OrderLineInput,
    PlaceOrderCommand,
)
from src.modules.ordering.application.ports import OfferPriceSnapshot  # noqa: E402
from src.modules.ordering.application.unit_of_work import (  # noqa: E402
    OrderingUnitOfWork,
)
from src.modules.ordering.application.use_cases import PlaceOrder  # noqa: E402
from src.modules.ordering.interface import router as ordering_router  # noqa: E402
from src.shared_kernel.application.errors import (  # noqa: E402
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)

OFFER = uuid.UUID("0ffe0000-0000-0000-0000-000000000001")
OFFER2 = uuid.UUID("0ffe0000-0000-0000-0000-000000000002")
CUSTOMER = uuid.UUID("cu570000-0000-0000-0000-000000000001".replace("cu57", "c057"))
ATTACKER = uuid.UUID("a77ac000-0000-0000-0000-000000000001")

REAL_PRICE = 1_000_000      # what the read model says
ATTACKER_PRICE = 1          # what a malicious client would like to pay


def run(coro):
    return asyncio.run(coro)


# --- doubles ---------------------------------------------------------------
class FakePricing:
    """Stands in for the BuyBox read model."""

    def __init__(self, snapshots: dict | None = None) -> None:
        self.snapshots = snapshots if snapshots is not None else {
            (OFFER, "ir"): OfferPriceSnapshot(
                offer_id=OFFER, region="ir", currency="IRR",
                amount=REAL_PRICE, scale=0, is_active=True,
            ),
        }
        self.calls: list[tuple] = []

    async def price_for(self, offer_id, region):
        self.calls.append((offer_id, region))
        return self.snapshots.get((offer_id, region))


class FakeOrderRepo:
    def __init__(self, uow):
        self._uow = uow
        self.added = []

    async def add(self, order):
        self.added.append(order)
        self._uow.track(order)

    async def get(self, order_id):
        return None

    async def save(self, order):
        self._uow.track(order)

    async def find_by_reservation_id(self, reservation_id):
        return None


class FakeUoW(OrderingUnitOfWork):
    def __init__(self):
        super().__init__()
        self.orders = FakeOrderRepo(self)

    async def commit(self):
        self.collect_new_events()
        self._committed = True

    async def rollback(self):
        self._discard_tracked()


def place_order(pricing=None) -> PlaceOrder:
    return PlaceOrder(uow_factory=FakeUoW, pricing=pricing or FakePricing())


def cmd(*, customer=CUSTOMER, region="ir", currency="IRR", lines=None):
    return PlaceOrderCommand(
        customer_id=str(customer), region=region, currency=currency,
        lines=lines or (OrderLineInput(offer_id=str(OFFER), location="default", quantity=2),),
    )


def snapshot(**over):
    base = dict(offer_id=OFFER, region="ir", currency="IRR",
                amount=REAL_PRICE, scale=0, is_active=True)
    base.update(over)
    return OfferPriceSnapshot(**base)


# === the client cannot choose the price ===================================
def test_the_command_has_no_field_in_which_to_send_a_price():
    """Strongest form of the fix: the price is not expressible as input, so no
    code path can accidentally trust one."""
    fields = {f.name for f in dataclasses.fields(OrderLineInput)}
    assert "unit_amount" not in fields
    assert fields == {"offer_id", "location", "quantity"}


def test_http_body_rejects_a_client_supplied_price():
    """extra='forbid' — an attempted bypass 422s instead of being ignored."""
    with pytest.raises(pydantic.ValidationError) as exc:
        ordering_router.LineBody(
            offer_id=str(OFFER), location="default",
            quantity=1, unit_amount=ATTACKER_PRICE,
        )
    assert "unit_amount" in str(exc.value)


def test_line_price_is_taken_from_the_read_model():
    pricing = FakePricing()
    view = run(place_order(pricing).execute(cmd()))

    assert view.lines[0].unit_amount == REAL_PRICE
    assert view.total_amount == REAL_PRICE * 2


def test_order_total_cannot_be_influenced_by_the_caller():
    """End-to-end intent of the fix: whatever the caller wanted to pay, the
    total is derived from server-side prices only."""
    pricing = FakePricing()
    view = run(place_order(pricing).execute(cmd(
        lines=(
            OrderLineInput(offer_id=str(OFFER), location="default", quantity=3),
        ),
    )))

    assert view.total_amount == REAL_PRICE * 3
    assert view.total_amount != ATTACKER_PRICE * 3


def test_the_pricing_gateway_is_consulted_for_every_line():
    pricing = FakePricing({
        (OFFER, "ir"): snapshot(),
        (OFFER2, "ir"): snapshot(offer_id=OFFER2, amount=250),
    })
    view = run(place_order(pricing).execute(cmd(lines=(
        OrderLineInput(offer_id=str(OFFER), location="default", quantity=1),
        OrderLineInput(offer_id=str(OFFER2), location="default", quantity=2),
    ))))

    assert pricing.calls == [(OFFER, "ir"), (OFFER2, "ir")]
    assert [ln.unit_amount for ln in view.lines] == [REAL_PRICE, 250]
    assert view.total_amount == REAL_PRICE + 500


def test_region_is_passed_to_the_gateway_so_pricing_is_region_scoped():
    pricing = FakePricing({(OFFER, "eu"): snapshot(region="eu", amount=42)})
    view = run(place_order(pricing).execute(cmd(region="eu")))

    assert pricing.calls == [(OFFER, "eu")]
    assert view.lines[0].unit_amount == 42


def test_an_offer_absent_from_the_region_cannot_be_ordered():
    with pytest.raises(NotFoundError):
        run(place_order(FakePricing({})).execute(cmd()))


def test_an_inactive_offer_cannot_be_ordered():
    """Suspended offers must not be sellable — the read model's is_active is the
    gate, and nothing else in Ordering knew about it before."""
    pricing = FakePricing({(OFFER, "ir"): snapshot(is_active=False)})
    with pytest.raises(ConflictError):
        run(place_order(pricing).execute(cmd()))


def test_an_unpriced_offer_cannot_be_ordered():
    pricing = FakePricing({(OFFER, "ir"): snapshot(amount=None)})
    with pytest.raises(ConflictError):
        run(place_order(pricing).execute(cmd()))


def test_currency_mismatch_is_rejected():
    pricing = FakePricing({(OFFER, "ir"): snapshot(currency="USD")})
    with pytest.raises(ValidationError):
        run(place_order(pricing).execute(cmd(currency="IRR")))


def test_mixed_price_scales_are_rejected_rather_than_mis_totalled():
    """total_amount sums raw minor units; 1.00 and 100 would silently add up."""
    pricing = FakePricing({
        (OFFER, "ir"): snapshot(scale=0, amount=100),
        (OFFER2, "ir"): snapshot(offer_id=OFFER2, scale=2, amount=100),
    })
    with pytest.raises(ValidationError):
        run(place_order(pricing).execute(cmd(lines=(
            OrderLineInput(offer_id=str(OFFER), location="default", quantity=1),
            OrderLineInput(offer_id=str(OFFER2), location="default", quantity=1),
        ))))


def test_nothing_is_persisted_when_pricing_rejects_a_line():
    """The ACL runs before the UoW opens, so a rejected order leaves no row and
    no OrderPlaced event."""
    pricing = FakePricing({(OFFER, "ir"): snapshot(is_active=False)})
    uows: list[FakeUoW] = []

    def factory():
        uow = FakeUoW()
        uows.append(uow)
        return uow

    with pytest.raises(ConflictError):
        run(PlaceOrder(uow_factory=factory, pricing=pricing).execute(cmd()))
    assert uows == []


def test_non_positive_quantity_is_a_validation_error_not_a_crash():
    with pytest.raises(ValidationError):
        run(place_order().execute(cmd(lines=(
            OrderLineInput(offer_id=str(OFFER), location="default", quantity=0),
        ))))


# === the client cannot impersonate another customer =======================
class FakeCurrentUser:
    def __init__(self, user_id):
        self.id = user_id
        self.roles = []


class CapturingPlaceOrder:
    def __init__(self):
        self.command = None

    async def execute(self, command):
        self.command = command
        return "ok"


def test_checkout_body_rejects_a_client_supplied_customer_id():
    with pytest.raises(pydantic.ValidationError) as exc:
        ordering_router.CheckoutBody(
            customer_id=str(ATTACKER), region="ir", currency="IRR",
            lines=[{"offer_id": str(OFFER), "quantity": 1}],
        )
    assert "customer_id" in str(exc.value)


def test_checkout_takes_the_customer_from_the_authenticated_principal():
    uc = CapturingPlaceOrder()
    body = ordering_router.CheckoutBody(
        region="ir", currency="IRR",
        lines=[{"offer_id": str(OFFER), "quantity": 1}],
    )

    run(ordering_router.checkout(body, uc, FakeCurrentUser(CUSTOMER)))

    assert uc.command.customer_id == str(CUSTOMER)


def test_two_principals_cannot_produce_orders_for_the_same_customer():
    """The attacker's own token is the only identity they can order under."""
    body = ordering_router.CheckoutBody(
        region="ir", currency="IRR",
        lines=[{"offer_id": str(OFFER), "quantity": 1}],
    )

    victim_uc, attacker_uc = CapturingPlaceOrder(), CapturingPlaceOrder()
    run(ordering_router.checkout(body, victim_uc, FakeCurrentUser(CUSTOMER)))
    run(ordering_router.checkout(body, attacker_uc, FakeCurrentUser(ATTACKER)))

    assert victim_uc.command.customer_id == str(CUSTOMER)
    assert attacker_uc.command.customer_id == str(ATTACKER)


def test_checkout_ignores_a_customer_id_even_if_one_reaches_the_body():
    """Defense in depth. extra='forbid' stops this at the schema today, so this
    passes a duck-typed body carrying customer_id to prove the endpoint itself
    never reads one — the schema and the handler must BOTH be safe."""
    class SmuggledBody:
        customer_id = str(ATTACKER)          # if the schema ever relaxes
        region = "ir"
        currency = "IRR"
        lines = [ordering_router.LineBody(offer_id=str(OFFER), quantity=1)]

    uc = CapturingPlaceOrder()
    run(ordering_router.checkout(SmuggledBody(), uc, FakeCurrentUser(CUSTOMER)))

    assert uc.command.customer_id == str(CUSTOMER)
    assert uc.command.customer_id != str(ATTACKER)


def test_checkout_requires_authentication():
    """The dependency must be declared, or the endpoint is anonymous."""
    import inspect

    from src.modules.identity.interface.dependencies import get_current_user

    params = inspect.signature(ordering_router.checkout).parameters
    assert params["current"].default.dependency is get_current_user


# === object-level authorization on reads ==================================
class _Order:
    def __init__(self, customer_id):
        self.customer_id = customer_id


def _get_order(order, principal):
    class Repo:
        def __init__(self, session):
            pass

        async def get(self, order_id):
            return order

    original = ordering_router.SqlOrderRepository
    ordering_router.SqlOrderRepository = Repo
    try:
        return run(ordering_router.get_order(uuid.uuid4(), None, FakeCurrentUser(principal)))
    finally:
        ordering_router.SqlOrderRepository = original


def test_a_customer_cannot_read_another_customers_order():
    with pytest.raises(ForbiddenError):
        _get_order(_Order(CUSTOMER), ATTACKER)


def test_a_customer_can_read_their_own_order():
    from src.modules.ordering.application.dtos import OrderView

    class Own:
        id = uuid.uuid4()
        customer_id = CUSTOMER
        region = "ir"
        currency = "IRR"
        status = "pending"
        lines: list = []
        total_amount = 0

    view = _get_order(Own(), CUSTOMER)
    assert isinstance(view, OrderView)
    assert view.customer_id == str(CUSTOMER)


def test_a_missing_order_is_not_found_not_forbidden():
    with pytest.raises(NotFoundError):
        _get_order(None, CUSTOMER)


# === boundary isolation ===================================================
def test_ordering_still_imports_no_other_context_except_the_identity_dependency():
    """The pricing ACL must stay raw SQL — importing BuyBox would couple the
    contexts at compile time and block extraction."""
    import pathlib
    import re

    root = pathlib.Path(ordering_router.__file__).resolve().parents[1]  # src/modules/ordering
    assert root.name == "ordering"
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        for module in re.findall(r"from (src\.modules\.\w+)", path.read_text(encoding="utf-8")):
            if module == "src.modules.ordering":
                continue
            # Identity's auth dependency is the one sanctioned interface-layer
            # import: it is how the principal reaches the endpoint.
            if module == "src.modules.identity" and path.name == "router.py":
                continue
            offenders.append(f"{path.name}: {module}")
    assert offenders == [], offenders
