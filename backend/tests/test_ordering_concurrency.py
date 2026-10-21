"""Ordering under concurrency and terminal-state races.

Two defects this pins shut:
  * orders_saga had no version, so two saga reactions for different lines could
    read the same state and the second silently overwrote the first. Correctness
    depended on the relay running exactly one replica.
  * a reserve that landed after the order already FAILED marked the line
    RESERVED and released nothing — real stock stranded until Inventory's TTL.

Run: pytest tests/test_ordering_concurrency.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.modules.ordering.application import use_cases as uc_module
from src.modules.ordering.application.unit_of_work import OrderingUnitOfWork
from src.modules.ordering.application.use_cases import (
    HandleInventoryExpired,
    HandleInventoryReservationFailed,
    HandleInventoryReserved,
    HandlePaymentSucceeded,
)
from src.modules.ordering.domain.entities import (
    LineStatus,
    Order,
    OrderLine,
    OrderStatus,
)
from src.shared_kernel.application.errors import ConcurrencyConflictError

OFFER_A = uuid.UUID("0ffe0000-0000-0000-0000-00000000000a")
OFFER_B = uuid.UUID("0ffe0000-0000-0000-0000-00000000000b")
LOC = "default"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(uc_module, "RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(uc_module, "RETRY_MAX_DELAY_SECONDS", 0.0)


def run(coro):
    return asyncio.run(coro)


def events(order) -> list[str]:
    return [type(e).__name__ for e in order.pull_events()]


def an_order(line_count: int = 2) -> Order:
    lines = [
        OrderLine(offer_id=OFFER_A, location=LOC, quantity=1, unit_amount=100),
        OrderLine(offer_id=OFFER_B, location=LOC, quantity=1, unit_amount=150),
    ][:line_count]
    order = Order.place(
        customer_id=uuid.uuid4(), region="ir", currency="IRR", lines=lines)
    order.pull_events()
    return order


# --- a store that mimics the version-guarded repository -------------------
class Store:
    """Holds the committed aggregate as plain data, so every read produces a
    fresh Order — exactly like the SQL repository's _to_domain."""

    def __init__(self, order: Order) -> None:
        self.rows = {
            order.id: {
                "customer_id": order.customer_id, "region": order.region,
                "currency": order.currency, "status": order.status,
                "version": order.version,
                "lines": [
                    {"offer_id": l.offer_id, "location": l.location,
                     "quantity": l.quantity, "unit_amount": l.unit_amount,
                     "reservation_id": l.reservation_id, "status": l.status}
                    for l in order.lines
                ],
            }
        }
        self.outbox: list = []
        self.conflicts_to_inject = 0

    def load(self, order_id) -> Order | None:
        rec = self.rows.get(order_id)
        if rec is None:
            return None
        return Order(
            id=order_id, customer_id=rec["customer_id"], region=rec["region"],
            currency=rec["currency"], status=rec["status"], version=rec["version"],
            lines=[
                OrderLine(
                    offer_id=l["offer_id"], location=l["location"],
                    quantity=l["quantity"], unit_amount=l["unit_amount"],
                    reservation_id=l["reservation_id"], status=l["status"],
                )
                for l in rec["lines"]
            ],
        )

    def order_id(self):
        return next(iter(self.rows))

    def status(self):
        return self.rows[self.order_id()]["status"]

    def line_statuses(self) -> list[str]:
        return [l["status"] for l in self.rows[self.order_id()]["lines"]]

    def emitted(self, name: str) -> int:
        return sum(1 for e in self.outbox if type(e).__name__ == name)

    def released_reservations(self) -> list[str]:
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
        order = self._st.load(order_id)
        if order is not None:
            self._uow.track(order)
        return order

    async def find_by_reservation_id(self, reservation_id):
        for oid, rec in self._st.rows.items():
            if any(l["reservation_id"] == reservation_id for l in rec["lines"]):
                return await self.get(oid)
        return None

    async def add(self, order):
        raise NotImplementedError

    async def save(self, order):
        if self._st.conflicts_to_inject > 0:
            self._st.conflicts_to_inject -= 1
            raise ConcurrencyConflictError("injected")
        rec = self._st.rows[order.id]
        if rec["version"] != order.version:          # optimistic lock
            raise ConcurrencyConflictError("order version conflict")
        rec["version"] = order.version + 1
        order.version += 1
        rec["status"] = order.status
        by_rid = {l["reservation_id"]: l for l in rec["lines"]}
        for ln in order.lines:
            if ln.reservation_id in by_rid:
                by_rid[ln.reservation_id]["status"] = ln.status
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


def factory_for(store: Store):
    return lambda: FakeUoW(store)


# === versioning ===========================================================
def test_a_new_order_starts_at_version_zero():
    assert an_order().version == 0


def test_a_successful_save_bumps_the_version():
    store = Store(an_order())
    order = store.load(store.order_id())
    order.on_line_reserved(order.lines[0].reservation_id)

    run(FakeOrderRepo(FakeUoW(store), store).save(order))

    assert order.version == 1
    assert store.rows[store.order_id()]["version"] == 1


def test_a_stale_writer_is_rejected():
    """Two readers at the same version; the second write must not land."""
    store = Store(an_order())
    uow = FakeUoW(store)
    a = store.load(store.order_id())
    b = store.load(store.order_id())          # stale copy, same version

    a.on_line_reserved(a.lines[0].reservation_id)
    run(FakeOrderRepo(uow, store).save(a))

    b.on_line_reserved(b.lines[1].reservation_id)
    with pytest.raises(ConcurrencyConflictError):
        run(FakeOrderRepo(uow, store).save(b))


# === concurrent reactions do not overwrite each other =====================
def test_concurrent_reservations_for_two_lines_both_survive():
    """The lost-update case: without the version guard the second reaction wrote
    its own line and reverted the first's."""
    store = Store(an_order())
    order = store.load(store.order_id())
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id
    handler = HandleInventoryReserved(factory_for(store))

    run(handler.execute(rid_a))
    run(handler.execute(rid_b))

    assert store.line_statuses() == [LineStatus.RESERVED, LineStatus.RESERVED]
    assert store.status() == OrderStatus.RESERVED
    assert store.emitted("OrderReserved") == 1
    assert store.emitted("PaymentRequested") == 1


def test_a_conflicted_reaction_retries_and_still_applies():
    store = Store(an_order())
    order = store.load(store.order_id())
    store.conflicts_to_inject = 1                 # first save loses the race

    run(HandleInventoryReserved(factory_for(store)).execute(order.lines[0].reservation_id))

    assert store.line_statuses()[0] == LineStatus.RESERVED
    assert store.conflicts_to_inject == 0


def test_a_conflicted_reaction_applies_exactly_once():
    """A retry must not double-apply: the reaction re-reads and re-decides."""
    store = Store(an_order())
    order = store.load(store.order_id())
    store.conflicts_to_inject = 2

    run(HandleInventoryReserved(factory_for(store)).execute(order.lines[0].reservation_id))

    assert store.line_statuses() == [LineStatus.RESERVED, LineStatus.PENDING]
    assert store.rows[store.order_id()]["version"] == 1     # one successful write
    assert store.emitted("OrderReserved") == 0              # other line still pending


def test_a_persistent_conflict_surfaces_after_the_retry_budget():
    store = Store(an_order())
    order = store.load(store.order_id())
    store.conflicts_to_inject = 99

    with pytest.raises(ConcurrencyConflictError):
        run(HandleInventoryReserved(factory_for(store)).execute(order.lines[0].reservation_id))

    assert store.line_statuses() == [LineStatus.PENDING, LineStatus.PENDING]
    assert store.outbox == []                                # nothing leaked


def test_a_failed_reaction_leaves_no_partial_state():
    store = Store(an_order())
    order = store.load(store.order_id())
    store.conflicts_to_inject = 99

    with pytest.raises(ConcurrencyConflictError):
        run(HandleInventoryReservationFailed(factory_for(store)).execute(
            order.lines[0].reservation_id))

    assert store.status() == OrderStatus.PENDING
    assert store.outbox == []


def test_reserve_racing_a_failure_does_not_resurrect_the_order():
    """Line A fails while line B's reserve is in flight. Whichever order they
    land in, the order must end FAILED and B must not stay held."""
    store = Store(an_order())
    order = store.load(store.order_id())
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id

    run(HandleInventoryReservationFailed(factory_for(store)).execute(rid_a))
    run(HandleInventoryReserved(factory_for(store)).execute(rid_b))

    assert store.status() == OrderStatus.FAILED
    assert LineStatus.RESERVED not in store.line_statuses()
    assert str(rid_b) in store.released_reservations()


# === late reserve on a terminal order does not leak stock =================
def test_late_reserve_on_a_failed_order_releases_instead_of_holding():
    order = an_order()
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id
    order.on_line_reservation_failed(rid_a)
    assert order.status == OrderStatus.FAILED
    order.pull_events()

    order.on_line_reserved(rid_b)               # arrives late

    line_b = order.lines[1]
    assert line_b.status == LineStatus.RELEASED
    assert line_b.status != LineStatus.RESERVED
    emitted = order.pull_events()
    assert [type(e).__name__ for e in emitted] == ["InventoryReleaseRequested"]
    assert emitted[0].reservation_ids == (str(rid_b),)
    assert emitted[0].reason == "order_already_terminal"


def test_late_reserve_on_a_cancelled_order_also_releases():
    order = an_order()
    order.on_line_reserved(order.lines[0].reservation_id)
    order.cancel()
    assert order.status == OrderStatus.CANCELLED
    order.pull_events()

    order.on_line_reserved(order.lines[1].reservation_id)

    assert order.lines[1].status == LineStatus.RELEASED
    assert order.pull_events()[0].reservation_ids == (str(order.lines[1].reservation_id),)


def test_late_reserve_does_not_move_a_terminal_order_back_to_reserved():
    order = an_order()
    order.on_line_reservation_failed(order.lines[0].reservation_id)
    order.pull_events()

    order.on_line_reserved(order.lines[1].reservation_id)

    assert order.status == OrderStatus.FAILED
    assert "OrderReserved" not in events(order)


def test_a_paid_order_keeps_its_holds():
    """PAID is terminal but NOT abandoned — releasing there would hand back
    stock the customer already paid for."""
    order = an_order(line_count=1)
    rid = order.lines[0].reservation_id
    order.on_line_reserved(rid)
    order.on_payment_succeeded()
    assert order.status == OrderStatus.PAID
    order.pull_events()

    order.on_line_reserved(rid)                 # redelivered

    assert order.lines[0].status == LineStatus.COMMITTED
    assert events(order) == []


def test_a_paid_order_never_hands_back_a_hold_even_defensively():
    """PAID must stay out of ABANDONED. The state is unreachable through the
    state machine (PAID requires every line RESERVED), so it is constructed
    directly — this pins the SET's contents, not the reachable path."""
    order = Order(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), region="ir", currency="IRR",
        status=OrderStatus.PAID,
        lines=[OrderLine(offer_id=OFFER_A, location=LOC, quantity=1, unit_amount=100)],
    )
    rid = order.lines[0].reservation_id
    assert order.lines[0].status == LineStatus.PENDING

    order.on_line_reserved(rid)

    assert order.lines[0].status != LineStatus.RELEASED
    assert events(order) == []          # a paid order gives nothing back


def test_no_stock_leak_end_to_end_through_the_handlers():
    """The real leak: line A is short (no hold ever existed) so the order fails
    immediately, then line B's reserve — a genuine hold — lands afterwards.
    Before the fix it sat RESERVED on a dead order until Inventory's TTL."""
    store = Store(an_order())
    order = store.load(store.order_id())
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id

    run(HandleInventoryReservationFailed(factory_for(store)).execute(rid_a))
    assert store.status() == OrderStatus.FAILED

    run(HandleInventoryReserved(factory_for(store)).execute(rid_b))   # late, real hold

    released = store.released_reservations()
    assert str(rid_b) in released          # handed straight back
    assert str(rid_a) not in released      # never held, nothing to release
    assert LineStatus.RESERVED not in store.line_statuses()
    assert store.line_statuses() == [LineStatus.FAILED, LineStatus.RELEASED]


def test_late_reserve_after_expiry_does_not_double_release():
    """Out-of-order delivery: InventoryExpired then InventoryReserved for the
    SAME reservation. Inventory's sweeper already returned that stock, so the
    line is FAILED and the reserve is correctly ignored. Emitting a release here
    would be a spurious second command — do not 'fix' this into one."""
    store = Store(an_order())
    order = store.load(store.order_id())
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id

    run(HandleInventoryReserved(factory_for(store)).execute(rid_a))
    run(HandleInventoryExpired(factory_for(store)).execute(rid_b))
    released_before = list(store.released_reservations())

    run(HandleInventoryReserved(factory_for(store)).execute(rid_b))

    assert store.released_reservations() == released_before
    assert str(rid_a) in released_before          # the sibling hold was handed back
    assert LineStatus.RESERVED not in store.line_statuses()


# === replay safety preserved =============================================
def test_replaying_a_reserve_is_a_no_op():
    store = Store(an_order())
    order = store.load(store.order_id())
    handler = HandleInventoryReserved(factory_for(store))

    run(handler.execute(order.lines[0].reservation_id))
    version_after_first = store.rows[store.order_id()]["version"]
    run(handler.execute(order.lines[0].reservation_id))

    assert store.line_statuses()[0] == LineStatus.RESERVED
    assert store.emitted("OrderReserved") == 0
    # A no-op reaction still writes (status unchanged), but must not double-apply.
    assert store.rows[store.order_id()]["version"] >= version_after_first


def test_replaying_the_late_release_does_not_release_twice():
    """The release path must itself be idempotent, or a redelivered reserve
    would emit a second InventoryReleaseRequested."""
    order = an_order()
    order.on_line_reservation_failed(order.lines[0].reservation_id)
    order.pull_events()
    rid_b = order.lines[1].reservation_id

    order.on_line_reserved(rid_b)
    first = order.pull_events()
    order.on_line_reserved(rid_b)               # redelivered
    second = order.pull_events()

    assert len(first) == 1
    assert second == []


def test_replaying_the_whole_reply_sequence_changes_nothing():
    store = Store(an_order())
    order = store.load(store.order_id())
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id
    reserved = HandleInventoryReserved(factory_for(store))
    paid = HandlePaymentSucceeded(factory_for(store))

    run(reserved.execute(rid_a))
    run(reserved.execute(rid_b))
    run(paid.execute(store.order_id()))
    snapshot = (store.status(), store.line_statuses(), len(store.outbox))

    run(reserved.execute(rid_a))
    run(reserved.execute(rid_b))
    run(paid.execute(store.order_id()))

    assert (store.status(), store.line_statuses(), len(store.outbox)) == snapshot
    assert store.emitted("OrderPaid") == 1
    assert store.emitted("InventoryCommitRequested") == 1


# === SQL adapter (statement-level, still DB-free) =========================
class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Row:
    lines: list = []


class CapturingSession:
    """Enough AsyncSession surface to compile and inspect the emitted SQL."""

    def __init__(self, rowcount: int = 1) -> None:
        self.statements: list = []
        self._rowcount = rowcount

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self._rowcount)

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return _Row()

    async def flush(self):
        return None


def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def _sql_repo(session):
    from src.modules.ordering.infrastructure.repository import SqlOrderRepository

    return SqlOrderRepository(session)


def test_sql_save_carries_the_version_predicate():
    """The real lost-update guard. FakeOrderRepo reimplements it, so only this
    test pins the actual UPDATE."""
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rowcount=1)
    run(_sql_repo(session).save(an_order()))

    assert "version = " in _sql(session.statements[0]).split("WHERE", 1)[1]


def test_sql_save_translates_a_lost_race_into_a_conflict():
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rowcount=0)       # another writer moved the row

    with pytest.raises(ConcurrencyConflictError):
        run(_sql_repo(session).save(an_order()))


def test_sql_save_bumps_the_in_memory_version():
    pytest.importorskip("sqlalchemy")
    order = an_order()
    run(_sql_repo(CapturingSession(rowcount=1)).save(order))

    assert order.version == 1
