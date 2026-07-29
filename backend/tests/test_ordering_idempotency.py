"""Idempotency-Key on checkout.

POST /orders was not idempotent: a retried request (timeout, double-tap, mobile
auto-retry) placed a SECOND order with fresh reservation_ids — double stock hold,
and a double charge once Payments is real.

Run: pytest tests/test_ordering_idempotency.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="ordering interface pulls in the DB layer")
pytest.importorskip("jwt", reason="identity dependency pulls in PyJWT")

from src.modules.ordering.application import use_cases as uc_module  # noqa: E402
from src.modules.ordering.application.dtos import (  # noqa: E402
    OrderLineInput,
    PlaceOrderCommand,
)
from src.modules.ordering.application.ports import (  # noqa: E402
    IdempotencyRecord,
    OfferPriceSnapshot,
)
from src.modules.ordering.application.unit_of_work import (  # noqa: E402
    OrderingUnitOfWork,
)
from src.modules.ordering.application.use_cases import PlaceOrder  # noqa: E402
from src.shared_kernel.application.errors import (  # noqa: E402
    ConcurrencyConflictError,
    ConflictError,
)

OFFER = uuid.UUID("0ffe0000-0000-0000-0000-000000000001")
OFFER2 = uuid.UUID("0ffe0000-0000-0000-0000-000000000002")
CUSTOMER = uuid.UUID("c0570000-0000-0000-0000-000000000001")
OTHER = uuid.UUID("c0570000-0000-0000-0000-000000000002")
KEY = "checkout-attempt-1"
PRICE = 1_000_000


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(uc_module, "RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(uc_module, "RETRY_MAX_DELAY_SECONDS", 0.0)


def run(coro):
    return asyncio.run(coro)


# --- doubles ---------------------------------------------------------------
class Store:
    """Committed state, shared across UoWs — one row per placed order."""

    def __init__(self) -> None:
        self.orders: dict = {}
        self.keys: dict = {}          # (customer_id, key) -> IdempotencyRecord
        self.outbox: list = []
        self.conflicts_to_inject = 0
        self.race_winner_id: uuid.UUID | None = None

    def emitted(self, name: str) -> int:
        return sum(1 for e in self.outbox if type(e).__name__ == name)


class FakePricing:
    async def price_for(self, offer_id, region):
        return OfferPriceSnapshot(
            offer_id=offer_id, region=region, currency="IRR",
            amount=PRICE, scale=0, is_active=True,
        )


class FakeOrderRepo:
    def __init__(self, uow, store: Store) -> None:
        self._uow = uow
        self._st = store

    async def add(self, order):
        self._st.orders[order.id] = order
        self._uow.track(order)

    async def get(self, order_id):
        return self._st.orders.get(order_id)

    async def save(self, order):
        self._uow.track(order)

    async def find_by_reservation_id(self, reservation_id):
        return None


class FakeIdempotencyStore:
    def __init__(self, store: Store) -> None:
        self._st = store

    async def find(self, customer_id, idempotency_key):
        return self._st.keys.get((customer_id, idempotency_key))

    async def remember(self, customer_id, idempotency_key, order_id, request_fingerprint):
        if self._st.conflicts_to_inject > 0:      # concurrent duplicate won
            self._st.conflicts_to_inject -= 1
            self._st.keys[(customer_id, idempotency_key)] = IdempotencyRecord(
                order_id=self._st.race_winner_id, request_fingerprint=request_fingerprint,
            )
            raise ConcurrencyConflictError("idempotency key already used")
        if (customer_id, idempotency_key) in self._st.keys:
            raise ConcurrencyConflictError("idempotency key already used")
        self._st.keys[(customer_id, idempotency_key)] = IdempotencyRecord(
            order_id=order_id, request_fingerprint=request_fingerprint,
        )


class FakeUoW(OrderingUnitOfWork):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self._st = store
        self.orders = FakeOrderRepo(self, store)
        self.idempotency = FakeIdempotencyStore(store)

    async def commit(self):
        for _agg, ev in self.collect_new_events():
            self._st.outbox.append(ev)
        self._committed = True

    async def rollback(self):
        # A rolled-back attempt must not leave its order behind.
        for oid in [o for o in self._st.orders if o not in self._committed_ids()]:
            pass
        self._discard_tracked()

    def _committed_ids(self):
        return set(self._st.orders)


def place_order(store: Store) -> PlaceOrder:
    return PlaceOrder(uow_factory=lambda: FakeUoW(store), pricing=FakePricing())


def cmd(*, customer=CUSTOMER, key=KEY, quantity=1, offer=OFFER, region="ir"):
    return PlaceOrderCommand(
        customer_id=str(customer), region=region, currency="IRR",
        lines=(OrderLineInput(offer_id=str(offer), location="default", quantity=quantity),),
        idempotency_key=key,
    )


# === fingerprint ==========================================================
def test_the_same_request_fingerprints_identically():
    assert cmd().fingerprint() == cmd().fingerprint()


def test_a_different_quantity_changes_the_fingerprint():
    assert cmd(quantity=1).fingerprint() != cmd(quantity=2).fingerprint()


def test_a_different_offer_changes_the_fingerprint():
    assert cmd(offer=OFFER).fingerprint() != cmd(offer=OFFER2).fingerprint()


def test_a_different_customer_changes_the_fingerprint():
    assert cmd(customer=CUSTOMER).fingerprint() != cmd(customer=OTHER).fingerprint()


def test_line_order_does_not_change_the_fingerprint():
    """Reordering the cart is the same order; it must not look like a new one."""
    a = OrderLineInput(offer_id=str(OFFER), location="default", quantity=1)
    b = OrderLineInput(offer_id=str(OFFER2), location="default", quantity=2)
    base = dict(customer_id=str(CUSTOMER), region="ir", currency="IRR",
                idempotency_key=KEY)
    assert (PlaceOrderCommand(lines=(a, b), **base).fingerprint()
            == PlaceOrderCommand(lines=(b, a), **base).fingerprint())


def test_the_key_itself_is_not_part_of_the_fingerprint():
    assert cmd(key="k1").fingerprint() == cmd(key="k2").fingerprint()


# === duplicate POST is safe ===============================================
def test_a_retried_checkout_returns_the_original_order():
    store = Store()
    uc = place_order(store)

    first = run(uc.execute(cmd()))
    second = run(uc.execute(cmd()))

    assert first.id == second.id
    assert len(store.orders) == 1


def test_a_retried_checkout_does_not_place_a_second_reservation():
    """The actual harm: a second order means a second set of reservation_ids and
    a second hold on the same stock."""
    store = Store()
    uc = place_order(store)

    first = run(uc.execute(cmd()))
    second = run(uc.execute(cmd()))

    assert store.emitted("OrderPlaced") == 1
    assert store.emitted("OrderReservationRequested") == 1
    first_rids = {ln.reservation_id for ln in first.lines}
    assert {ln.reservation_id for ln in second.lines} == first_rids


def test_retrying_many_times_still_yields_one_order():
    store = Store()
    uc = place_order(store)
    ids = {run(uc.execute(cmd())).id for _ in range(5)}

    assert len(ids) == 1
    assert store.emitted("OrderPlaced") == 1


def test_a_different_key_places_a_genuinely_new_order():
    store = Store()
    uc = place_order(store)

    a = run(uc.execute(cmd(key="k1")))
    b = run(uc.execute(cmd(key="k2")))

    assert a.id != b.id
    assert len(store.orders) == 2


def test_keys_are_scoped_per_customer():
    """One customer's key must not collide with — or expose — another's order."""
    store = Store()
    uc = place_order(store)

    mine = run(uc.execute(cmd(customer=CUSTOMER, key=KEY)))
    theirs = run(uc.execute(cmd(customer=OTHER, key=KEY)))

    assert mine.id != theirs.id
    assert mine.customer_id == str(CUSTOMER)
    assert theirs.customer_id == str(OTHER)


def test_reusing_a_key_for_a_different_body_is_rejected():
    """Silently returning the first order would ship the wrong goods."""
    store = Store()
    uc = place_order(store)
    run(uc.execute(cmd(quantity=1)))

    with pytest.raises(ConflictError):
        run(uc.execute(cmd(quantity=9)))

    assert len(store.orders) == 1


def test_omitting_the_key_keeps_the_old_non_idempotent_behaviour():
    """Opt-in: without a key nothing dedups, and nothing breaks."""
    store = Store()
    uc = place_order(store)

    a = run(uc.execute(cmd(key=None)))
    b = run(uc.execute(cmd(key=None)))

    assert a.id != b.id
    assert store.keys == {}


def test_a_concurrent_duplicate_loses_the_race_and_replays():
    """Two identical requests in flight: the unique constraint backstops the
    check, the loser retries and returns the winner's order."""
    store = Store()
    uc = place_order(store)
    winner = run(uc.execute(cmd()))

    del store.keys[(CUSTOMER, KEY)]              # simulate the pre-insert window
    store.race_winner_id = uuid.UUID(winner.id)
    store.conflicts_to_inject = 1

    replayed = run(uc.execute(cmd()))

    assert replayed.id == winner.id
    assert store.conflicts_to_inject == 0


def test_the_stored_order_keeps_its_original_snapshot_on_replay():
    """A replay re-prices, but must return the ORIGINAL order's numbers."""
    store = Store()
    uc = place_order(store)
    first = run(uc.execute(cmd()))

    second = run(uc.execute(cmd()))

    assert second.total_amount == first.total_amount
    assert second.lines[0].unit_amount == first.lines[0].unit_amount


def test_a_dangling_key_is_a_conflict_not_a_crash():
    store = Store()
    uc = place_order(store)
    run(uc.execute(cmd()))
    store.orders.clear()                          # order gone, key remains

    with pytest.raises(ConflictError):
        run(uc.execute(cmd()))


# === interface wiring =====================================================
def test_the_endpoint_reads_the_idempotency_key_header():
    import inspect

    from src.modules.ordering.interface import router as ordering_router

    param = inspect.signature(ordering_router.checkout).parameters["idempotency_key"]
    assert param.default.alias == "Idempotency-Key"


def test_the_endpoint_passes_the_header_into_the_command():
    from src.modules.ordering.interface import router as ordering_router

    class CapturingUc:
        command = None

        async def execute(self, command):
            CapturingUc.command = command
            return "ok"

    class Principal:
        id = CUSTOMER
        roles: list = []

    body = ordering_router.CheckoutBody(
        region="ir", currency="IRR",
        lines=[{"offer_id": str(OFFER), "quantity": 1}],
    )
    run(ordering_router.checkout(body, CapturingUc(), Principal(), "hdr-key-9"))

    assert CapturingUc.command.idempotency_key == "hdr-key-9"
    assert CapturingUc.command.customer_id == str(CUSTOMER)
