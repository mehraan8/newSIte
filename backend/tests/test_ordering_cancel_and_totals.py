"""Cancel path (O7) and persisted-total correctness (O8).

O7: Order.cancel() existed, compensated correctly and was in the status CHECK —
    but no endpoint reached it, so a customer could never release a hold early.
O8: total_amount was written on insert and never again. Lines are immutable
    today so it could not actually drift, but the column was free to go stale the
    moment anything mutated a line.

Run: pytest tests/test_ordering_cancel_and_totals.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="ordering interface pulls in the DB layer")
pytest.importorskip("jwt", reason="identity dependency pulls in PyJWT")

from src.modules.ordering.application import use_cases as uc_module  # noqa: E402
from src.modules.ordering.application.unit_of_work import (  # noqa: E402
    OrderingUnitOfWork,
)
from src.modules.ordering.application.use_cases import (  # noqa: E402
    CancelOrder,
    HandleInventoryReserved,
)
from src.modules.ordering.domain.entities import (  # noqa: E402
    LineStatus,
    Order,
    OrderLine,
    OrderStatus,
)
from src.shared_kernel.application.errors import (  # noqa: E402
    ConflictError,
    ForbiddenError,
    NotFoundError,
)

OFFER_A = uuid.UUID("0ffe0000-0000-0000-0000-00000000000a")
OFFER_B = uuid.UUID("0ffe0000-0000-0000-0000-00000000000b")
CUSTOMER = uuid.UUID("c0570000-0000-0000-0000-000000000001")
ATTACKER = uuid.UUID("a77ac000-0000-0000-0000-000000000001")
LOC = "default"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(uc_module, "RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(uc_module, "RETRY_MAX_DELAY_SECONDS", 0.0)


def run(coro):
    return asyncio.run(coro)


def an_order(customer=CUSTOMER) -> Order:
    order = Order.place(
        customer_id=customer, region="ir", currency="IRR",
        lines=[
            OrderLine(offer_id=OFFER_A, location=LOC, quantity=2, unit_amount=100),
            OrderLine(offer_id=OFFER_B, location=LOC, quantity=1, unit_amount=150),
        ],
    )
    order.pull_events()
    return order


# --- doubles (the store persists total_amount like the real column) --------
class Store:
    def __init__(self, order: Order) -> None:
        self.order = order
        self.persisted_total: int | None = order.total_amount
        self.persisted_status = order.status
        self.version = order.version
        self.outbox: list = []

    def emitted(self, name: str) -> int:
        return sum(1 for e in self.outbox if type(e).__name__ == name)

    def released(self) -> list[str]:
        out = []
        for e in self.outbox:
            if type(e).__name__ == "InventoryReleaseRequested":
                out.extend(e.reservation_ids)
        return out


class FakeOrderRepo:
    def __init__(self, uow, store: Store) -> None:
        self._uow = uow
        self._st = store

    async def get(self, order_id):
        if order_id != self._st.order.id:
            return None
        self._uow.track(self._st.order)
        return self._st.order

    async def find_by_reservation_id(self, reservation_id):
        if any(l.reservation_id == reservation_id for l in self._st.order.lines):
            return await self.get(self._st.order.id)
        return None

    async def add(self, order):
        raise NotImplementedError

    async def save(self, order):
        # Mirrors the real UPDATE: status, total_amount and version together.
        self._st.persisted_status = order.status
        self._st.persisted_total = order.total_amount
        self._st.version = order.version + 1
        order.version += 1
        self._uow.track(order)


class FakeUoW(OrderingUnitOfWork):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self._st = store
        self.orders = FakeOrderRepo(self, store)

    async def commit(self):
        for _agg, ev in self.collect_new_events():
            self._st.outbox.append(ev)
        self._committed = True

    async def rollback(self):
        self._discard_tracked()


def cancel_uc(store: Store) -> CancelOrder:
    return CancelOrder(uow_factory=lambda: FakeUoW(store))


# === O7: cancel is reachable and safe =====================================
def test_a_pending_order_can_be_cancelled():
    store = Store(an_order())

    view = run(cancel_uc(store).execute(store.order.id, customer_id=CUSTOMER))

    assert view.status == OrderStatus.CANCELLED
    assert store.persisted_status == OrderStatus.CANCELLED
    assert store.emitted("OrderCancelled") == 1


def test_cancelling_a_reserved_order_hands_back_its_holds():
    """The point of the endpoint: release stock early instead of waiting for TTL."""
    store = Store(an_order())
    order = store.order
    order.on_line_reserved(order.lines[0].reservation_id)
    order.on_line_reserved(order.lines[1].reservation_id)
    order.pull_events()

    run(cancel_uc(store).execute(order.id, customer_id=CUSTOMER))

    released = store.released()
    assert {str(l.reservation_id) for l in order.lines} == set(released)
    assert all(l.status == LineStatus.RELEASED for l in order.lines)


def test_cancelling_is_idempotent():
    store = Store(an_order())
    uc = cancel_uc(store)

    run(uc.execute(store.order.id, customer_id=CUSTOMER))
    run(uc.execute(store.order.id, customer_id=CUSTOMER))

    assert store.emitted("OrderCancelled") == 1


def test_a_paid_order_cannot_be_cancelled():
    """Reversing a paid order is a refund — a different process Payments owns."""
    store = Store(an_order())
    order = store.order
    for line in order.lines:
        order.on_line_reserved(line.reservation_id)
    order.on_payment_succeeded()
    assert order.status == OrderStatus.PAID
    order.pull_events()

    with pytest.raises(ConflictError):
        run(cancel_uc(store).execute(order.id, customer_id=CUSTOMER))

    assert store.emitted("OrderCancelled") == 0


def test_another_customer_cannot_cancel_my_order():
    """Authorization is enforced in the USE CASE, not only the router."""
    store = Store(an_order(customer=CUSTOMER))

    with pytest.raises(ForbiddenError):
        run(cancel_uc(store).execute(store.order.id, customer_id=ATTACKER))

    assert store.persisted_status == OrderStatus.PENDING
    assert store.outbox == []


def test_cancelling_an_unknown_order_is_not_found():
    store = Store(an_order())
    with pytest.raises(NotFoundError):
        run(cancel_uc(store).execute(uuid.uuid4(), customer_id=CUSTOMER))


def test_a_reserve_landing_after_cancel_is_handed_straight_back():
    """Cancelling while a line is still in flight must not strand that hold —
    the O10 ABANDONED guard covers the CANCELLED state too."""
    store = Store(an_order())
    order = store.order
    rid_late = order.lines[1].reservation_id

    run(cancel_uc(store).execute(order.id, customer_id=CUSTOMER))
    run(HandleInventoryReserved(lambda: FakeUoW(store)).execute(rid_late))

    assert order.lines[1].status == LineStatus.RELEASED
    assert str(rid_late) in store.released()
    assert LineStatus.RESERVED not in [l.status for l in order.lines]


def test_cancel_is_wired_to_an_authenticated_endpoint():
    import inspect

    from src.modules.identity.interface.dependencies import get_current_user
    from src.modules.ordering.interface import router as ordering_router

    params = inspect.signature(ordering_router.cancel_order).parameters
    assert params["current"].default.dependency is get_current_user
    assert any(
        getattr(r, "path", "") == "/orders/{order_id}/cancel"
        for r in ordering_router.router.routes
    )


def test_no_dead_cancel_path_remains():
    """cancel() is reachable from the API, so it is not dead code."""
    from src.modules.ordering.interface import router as ordering_router

    assert hasattr(ordering_router, "cancel_order")
    assert hasattr(ordering_router, "get_cancel_order")


# === O8: persisted total tracks the lines =================================
def test_total_amount_is_derived_from_the_lines():
    order = an_order()
    assert order.total_amount == 2 * 100 + 1 * 150


def test_a_saved_order_rewrites_its_total():
    """The column is a denormalisation; every write re-materialises it."""
    store = Store(an_order())
    store.persisted_total = 999_999            # a stale value from anywhere

    run(cancel_uc(store).execute(store.order.id, customer_id=CUSTOMER))

    assert store.persisted_total == store.order.total_amount


def test_a_stale_total_cannot_survive_a_saga_reaction():
    store = Store(an_order())
    order = store.order
    store.persisted_total = 0                  # drifted

    run(HandleInventoryReserved(lambda: FakeUoW(store)).execute(
        order.lines[0].reservation_id))

    assert store.persisted_total == order.total_amount


def test_line_status_changes_do_not_change_the_total():
    """Lines are immutable except for status, so the total is invariant across
    the whole saga — this pins that so a future line mutation is caught."""
    order = an_order()
    before = order.total_amount
    for line in order.lines:
        order.on_line_reserved(line.reservation_id)
    order.on_payment_succeeded()

    assert order.total_amount == before


def test_the_sql_update_writes_total_amount():
    """Statement-level: the real UPDATE must carry the column, or the fix only
    exists in the fake."""
    from src.modules.ordering.infrastructure.repository import SqlOrderRepository

    class _Result:
        rowcount = 1

    class _Row:
        lines: list = []

    class CapturingSession:
        def __init__(self):
            self.statements = []

        async def execute(self, stmt):
            self.statements.append(stmt)
            return _Result()

        async def scalar(self, stmt):
            return _Row()

        async def flush(self):
            return None

    from sqlalchemy.dialects import postgresql

    session = CapturingSession()
    run(SqlOrderRepository(session).save(an_order()))
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))

    assert "total_amount" in sql.split("WHERE", 1)[0]
