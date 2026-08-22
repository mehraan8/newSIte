"""Inventory tests (DB-free) — the first hard-consistency context.

Real domain aggregate, real use cases including the retry loop. The fake
repository is *transactional*: writes are staged in a per-UoW overlay and only
reach the store on commit, so rollback semantics and event leakage are
observable (a write-through fake cannot express either).

Run: pytest tests/test_inventory_flow.py
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from src.modules.inventory.application import use_cases as uc_module
from src.modules.inventory.application.dtos import (
    ReceiveStockCommand,
    ReserveStockCommand,
)
from src.modules.inventory.application.unit_of_work import InventoryUnitOfWork
from src.modules.inventory.application.use_cases import (
    CommitForOrder,
    CommitReservation,
    ExpireReservations,
    ReceiveStock,
    ReleaseReservation,
    ReserveForOrder,
    ReserveStock,
)
from src.modules.inventory.domain.entities import (
    InventoryItem,
    Reservation,
    ReservationStatus,
)
from src.shared_kernel.application.errors import (
    ConcurrencyConflictError,
    ConflictError,
    NotFoundError,
)
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal
from src.shared_kernel.domain.base import utcnow
from src.shared_kernel.infrastructure.outbox.serialization import event_payload

OFFER = uuid.UUID("0ffe0000-0000-0000-0000-000000000001")
OFFER2 = uuid.UUID("0ffe0000-0000-0000-0000-000000000002")
LOC = "default"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Keep the backoff logic exercised but instant."""
    monkeypatch.setattr(uc_module, "RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(uc_module, "RETRY_MAX_DELAY_SECONDS", 0.0)


def run(coro):
    return asyncio.run(coro)


# --- transactional fake persistence ---------------------------------------
class Store:
    """Committed state only. Nothing lands here except through UoW.commit()."""

    def __init__(self) -> None:
        self.items: dict = {}
        self.reservations: dict = {}
        self.outbox: list = []
        self.force_save_conflicts = 0
        self.persistent_conflict_offers: set = set()

    def events(self, name: str) -> int:
        return sum(1 for e in self.outbox if type(e).__name__ == name)

    def reserved(self, offer=OFFER, loc=LOC) -> int:
        return self.items[(offer, loc)]["reserved"]


class FakeRepo:
    """Mirrors SqlInventoryRepository: staged writes, copy-on-read domain objects,
    version-guarded save_item, status-guarded save_reservation."""

    def __init__(self, uow: "FakeInvUoW", store: Store) -> None:
        self._uow = uow
        self._st = store

    # -- merged (committed + staged) views --
    def _item_rec(self, key):
        return self._uow.staged_items.get(key, self._st.items.get(key))

    def _res_rec(self, rid):
        return self._uow.staged_res.get(rid, self._st.reservations.get(rid))

    def _all_res(self) -> dict:
        merged = dict(self._st.reservations)
        merged.update(self._uow.staged_res)
        return merged

    # -- inventory item --
    async def get_item(self, offer_id, location):
        rec = self._item_rec((offer_id, location))
        if not rec:
            return None
        return InventoryItem(
            id=rec["id"], offer_id=offer_id, location=location,
            on_hand=rec["on_hand"], reserved=rec["reserved"], version=rec["version"],
        )

    async def add_item(self, item):
        key = (item.offer_id, item.location)
        if self._item_rec(key) is not None:
            raise ConcurrencyConflictError()
        self._uow.staged_items[key] = {
            "id": item.id, "on_hand": item.on_hand,
            "reserved": item.reserved, "version": item.version,
        }
        self._uow.track(item)

    async def save_item(self, item):
        if self._st.force_save_conflicts > 0:
            self._st.force_save_conflicts -= 1
            raise ConcurrencyConflictError()          # injected concurrent writer
        if item.offer_id in self._st.persistent_conflict_offers:
            raise ConcurrencyConflictError()          # never succeeds
        key = (item.offer_id, item.location)
        rec = self._item_rec(key)
        if rec["version"] != item.version:            # optimistic lock
            raise ConcurrencyConflictError()
        self._uow.staged_items[key] = {
            "id": rec["id"], "on_hand": item.on_hand,
            "reserved": item.reserved, "version": item.version + 1,
        }
        item.version += 1
        self._uow.track(item)

    # -- reservation --
    async def get_reservation(self, rid):
        rec = self._res_rec(rid)
        if not rec:
            return None
        return Reservation(
            id=rid, offer_id=rec["offer_id"], location=rec["location"],
            quantity=rec["quantity"], status=rec["status"], expires_at=rec["expires_at"],
        )

    async def add_reservation(self, r):
        if self._res_rec(r.id) is not None:
            raise ConcurrencyConflictError()
        self._uow.staged_res[r.id] = {
            "offer_id": r.offer_id, "location": r.location, "quantity": r.quantity,
            "status": r.status, "expires_at": r.expires_at,
        }

    async def save_reservation(self, r, *, expected_status):
        rec = self._res_rec(r.id)
        if rec is None or rec["status"] != expected_status:
            raise ConcurrencyConflictError("reservation status conflict")
        self._uow.staged_res[r.id] = {**rec, "status": r.status}

    async def find_expired_pending(self, now, limit=100):
        due = [
            (rid, rec) for rid, rec in self._all_res().items()
            if rec["status"] == ReservationStatus.PENDING
            and rec["expires_at"] is not None
            and rec["expires_at"] <= now
        ]
        due.sort(key=lambda pair: pair[1]["expires_at"])
        return [
            Reservation(
                id=rid, offer_id=rec["offer_id"], location=rec["location"],
                quantity=rec["quantity"], status=rec["status"], expires_at=rec["expires_at"],
            )
            for rid, rec in due[:limit]
        ]


class FakeInvUoW(InventoryUnitOfWork):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self._st = store
        self.staged_items: dict = {}
        self.staged_res: dict = {}
        self.inventory = FakeRepo(self, store)

    async def commit(self):
        for _agg, ev in self.collect_new_events():
            event_payload(ev)                 # proves the event serializes PII-clean
            self._st.outbox.append(ev)
        self._st.items.update(self.staged_items)
        self._st.reservations.update(self.staged_res)
        self.staged_items, self.staged_res = {}, {}
        self._committed = True

    async def rollback(self):
        self.staged_items, self.staged_res = {}, {}
        self._discard_tracked()


class FakeSelling:
    async def offer_exists(self, offer_id):
        return offer_id in (OFFER, OFFER2)

    async def seller_of(self, offer_id):
        return SELLER if offer_id in (OFFER, OFFER2) else None


SELLER = uuid.uuid4()
# These suites validate the inventory LIFECYCLE, not authorization, so their
# principal is a platform operator holding `inventory.manage.any`. Authorization
# itself is proved in tests/test_inventory_authorization.py and
# tests/api/test_admin_authorization.py; here it must simply not get in the way,
# and passing a real Principal keeps this exercising the same code path rather
# than a bypass.
OPERATOR = Principal(
    subject_id=uuid.uuid4(),
    platform_permissions=frozenset({Permission.INVENTORY_MANAGE_ANY}),
)


def build(store: Store):
    factory = lambda: FakeInvUoW(store)  # noqa: E731
    return {
        "receive": ReceiveStock(uow_factory=factory, selling=FakeSelling()),
        "reserve": ReserveStock(uow_factory=factory),
        "commit": CommitReservation(uow_factory=factory),
        "release": ReleaseReservation(uow_factory=factory),
        "commit_saga": CommitForOrder(uow_factory=factory),
        "reserve_saga": ReserveForOrder(uow_factory=factory),
        "expire": ExpireReservations(uow_factory=factory),
    }


def stocked(qty: int = 10, offer=OFFER) -> tuple[Store, dict]:
    store = Store()
    ucs = build(store)
    run(ucs["receive"].execute(
        ReceiveStockCommand(offer_id=str(offer), location=LOC, quantity=qty), OPERATOR))
    return store, ucs


def reserve_cmd(rid, qty, offer=OFFER, ttl=900):
    return ReserveStockCommand(
        reservation_id=str(rid), offer_id=str(offer), location=LOC,
        quantity=qty, ttl_seconds=ttl,
    )


# === requirement: reserve success =========================================
def test_receive_then_reserve_holds_stock_and_emits_reserved():
    store, ucs = stocked(10)
    rid = uuid.uuid4()

    view = run(ucs["reserve"].execute(reserve_cmd(rid, 8)))

    assert view.status == ReservationStatus.PENDING
    assert store.reserved() == 8
    assert store.items[(OFFER, LOC)]["on_hand"] == 10   # holds do not consume
    assert store.events("InventoryReserved") == 1


def test_reserve_against_unknown_offer_is_not_found():
    store = Store()
    ucs = build(store)
    with pytest.raises(NotFoundError):
        run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 1)))
    assert store.reservations == {} and store.outbox == []


# === requirement: reserve over limit fails ================================
def test_reserve_beyond_available_is_rejected_and_changes_nothing():
    store, ucs = stocked(10)
    run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 8)))

    with pytest.raises(ConflictError):
        run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 5)))   # only 2 free

    assert store.reserved() == 8
    assert store.events("InventoryReserved") == 1
    assert len(store.reservations) == 1


def test_reserve_exactly_available_succeeds_and_next_unit_fails():
    store, ucs = stocked(3)
    run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 3)))
    assert store.reserved() == 3

    with pytest.raises(ConflictError):
        run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 1)))
    assert store.reserved() == 3


def test_reserved_never_exceeds_on_hand_across_many_holds():
    store, ucs = stocked(5)
    granted = 0
    for _ in range(10):
        try:
            run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 1)))
            granted += 1
        except ConflictError:
            pass
    assert granted == 5
    rec = store.items[(OFFER, LOC)]
    assert rec["reserved"] == 5 and rec["reserved"] <= rec["on_hand"]


# === requirement: duplicate reservation is idempotent =====================
def test_duplicate_reservation_id_is_a_no_op():
    store, ucs = stocked(10)
    rid = uuid.uuid4()

    first = run(ucs["reserve"].execute(reserve_cmd(rid, 8)))
    second = run(ucs["reserve"].execute(reserve_cmd(rid, 8)))

    assert first.id == second.id == str(rid)
    assert store.reserved() == 8                      # counted once
    assert store.events("InventoryReserved") == 1     # published once


def test_duplicate_reservation_with_a_different_quantity_does_not_re_reserve():
    """The id is the idempotency key; a conflicting body must not double-hold."""
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 4)))

    again = run(ucs["reserve"].execute(reserve_cmd(rid, 9)))

    assert again.quantity == 4
    assert store.reserved() == 4


def test_commit_and_release_are_idempotent():
    store, ucs = stocked(10)
    rid, rid2 = uuid.uuid4(), uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 8)))

    run(ucs["commit"].execute(rid))
    run(ucs["commit"].execute(rid))
    rec = store.items[(OFFER, LOC)]
    assert (rec["on_hand"], rec["reserved"]) == (2, 0)
    assert store.events("InventoryCommitted") == 1

    run(ucs["reserve"].execute(reserve_cmd(rid2, 2)))
    run(ucs["release"].execute(rid2))
    run(ucs["release"].execute(rid2))
    assert store.reserved() == 0
    assert store.events("InventoryReleased") == 1


def test_committed_reservation_cannot_be_released():
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 8)))
    run(ucs["commit"].execute(rid))

    with pytest.raises(ConflictError):
        run(ucs["release"].execute(rid))
    assert store.events("InventoryReleased") == 0


# === requirement: concurrent conflict fails correctly =====================
def test_stale_write_is_rejected_by_the_version_guard():
    """Two readers at the same version; the second write must not land."""
    store, _ = stocked(5)
    uow = FakeInvUoW(store)
    a = run(uow.inventory.get_item(OFFER, LOC))
    b = run(uow.inventory.get_item(OFFER, LOC))       # stale copy, same version

    a.reserve(1, uuid.uuid4())
    run(uow.inventory.save_item(a))
    b.reserve(1, uuid.uuid4())

    with pytest.raises(ConcurrencyConflictError):
        run(uow.inventory.save_item(b))


def test_transient_conflict_is_retried_and_reserves_exactly_once():
    store, ucs = stocked(10)
    store.force_save_conflicts = 1                   # first save loses the race

    view = run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 3)))

    assert view.status == ReservationStatus.PENDING
    assert store.force_save_conflicts == 0
    assert store.reserved() == 3                     # applied once, not twice
    assert store.events("InventoryReserved") == 1    # failed attempt published nothing


def test_persistent_conflict_surfaces_after_the_retry_budget():
    store, ucs = stocked(10)
    store.force_save_conflicts = 99                  # always loses

    with pytest.raises(ConcurrencyConflictError):
        run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 3)))

    assert store.reserved() == 0
    assert store.reservations == {} and store.outbox == []


def test_reservation_status_write_is_compare_and_set():
    """save_reservation must reject a transition from an unexpected status."""
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 2)))
    run(ucs["commit"].execute(rid))                  # now COMMITTED

    uow = FakeInvUoW(store)
    res = run(uow.inventory.get_reservation(rid))
    with pytest.raises(ConcurrencyConflictError):
        run(uow.inventory.save_reservation(res, expected_status=ReservationStatus.PENDING))


def test_concurrent_commit_and_release_cannot_both_win():
    """Both readers see PENDING; the version guard on the item serialises them."""
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 4)))

    run(ucs["commit"].execute(rid))                  # winner
    with pytest.raises(ConflictError):               # loser re-reads: COMMITTED
        run(ucs["release"].execute(rid))

    rec = store.items[(OFFER, LOC)]
    assert (rec["on_hand"], rec["reserved"]) == (6, 0)


# === requirement: rollback leaves no dirty state ==========================
def test_failed_reservation_leaves_no_state_and_no_events():
    """Mutation happened in-memory (reserved += 8) before the failure; none of it
    may reach the store, and no InventoryReserved may escape to the outbox."""
    store, ucs = stocked(10)
    rid = uuid.uuid4()

    boom = RuntimeError("writer exploded mid-transaction")
    original = FakeRepo.add_reservation

    async def explode(self, r):
        await original(self, r)          # stage it, then fail after the mutation
        raise boom

    FakeRepo.add_reservation = explode
    try:
        with pytest.raises(RuntimeError):
            run(ucs["reserve"].execute(reserve_cmd(rid, 8)))
    finally:
        FakeRepo.add_reservation = original

    assert store.reserved() == 0
    assert store.reservations == {}
    assert store.outbox == []


def test_rollback_does_not_leak_events_into_a_later_commit():
    """_discard_tracked must drain the failed attempt's events, or the next
    successful operation republishes them."""
    store, ucs = stocked(10)

    uow = FakeInvUoW(store)
    item = run(uow.inventory.get_item(OFFER, LOC))
    item.reserve(5, uuid.uuid4())
    run(uow.inventory.save_item(item))
    run(uow.rollback())

    assert store.outbox == []
    assert store.reserved() == 0

    run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 1)))
    assert store.events("InventoryReserved") == 1     # not 2


def test_uncommitted_unit_of_work_stages_nothing():
    store, _ = stocked(10)
    uow = FakeInvUoW(store)

    async def scenario():
        async with uow:
            item = await uow.inventory.get_item(OFFER, LOC)
            item.reserve(3, uuid.uuid4())
            await uow.inventory.save_item(item)
            # no commit -> __aexit__ rolls back

    run(scenario())
    assert store.reserved() == 0
    assert store.outbox == []


# === expiry (ADR-0009 D1) =================================================
def expired_reservation(store, ucs, qty=4, offer=OFFER):
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, qty, offer=offer, ttl=900)))
    store.reservations[rid]["expires_at"] = utcnow() - timedelta(seconds=1)
    return rid


def test_expiry_emits_inventory_expired_not_inventory_released():
    """A distinct event type: consumers subscribe by type, and the saga's reaction
    to a lapsed hold is the opposite of its reaction to its own compensation."""
    store, ucs = stocked(10)
    rid = expired_reservation(store, ucs)

    swept = run(ucs["expire"].execute())

    assert swept == 1
    assert store.reservations[rid]["status"] == ReservationStatus.EXPIRED
    assert store.reserved() == 0
    assert store.events("InventoryExpired") == 1
    assert store.events("InventoryReleased") == 0


def test_expiry_returns_stock_to_available():
    store, ucs = stocked(10)
    expired_reservation(store, ucs, qty=10)
    run(ucs["expire"].execute())

    run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 10)))   # must fit again
    assert store.reserved() == 10


def test_sweeper_is_idempotent_and_skips_non_pending():
    store, ucs = stocked(10)
    expired_reservation(store, ucs)

    assert run(ucs["expire"].execute()) == 1
    assert run(ucs["expire"].execute()) == 0
    assert store.events("InventoryExpired") == 1


def test_unexpired_reservations_are_left_alone():
    store, ucs = stocked(10)
    run(ucs["reserve"].execute(reserve_cmd(uuid.uuid4(), 4, ttl=900)))

    assert run(ucs["expire"].execute()) == 0
    assert store.reserved() == 4


def test_one_reservations_conflict_does_not_roll_back_another():
    """Per-reservation UoW: the batch-scoped sweeper lost every reservation in the
    pass when a single item was contended."""
    store, ucs = stocked(10)
    run(ucs["receive"].execute(
        ReceiveStockCommand(offer_id=str(OFFER2), location=LOC, quantity=10), OPERATOR))

    good = expired_reservation(store, ucs, qty=4, offer=OFFER)
    store.reservations[good]["expires_at"] = utcnow() - timedelta(seconds=99)  # swept first
    bad = expired_reservation(store, ucs, qty=4, offer=OFFER2)
    store.persistent_conflict_offers.add(OFFER2)

    swept = run(ucs["expire"].execute())

    assert swept == 1
    assert store.reservations[good]["status"] == ReservationStatus.EXPIRED
    assert store.reserved(OFFER) == 0
    assert store.reservations[bad]["status"] == ReservationStatus.PENDING
    assert store.reserved(OFFER2) == 4          # untouched, retried next pass


def test_committing_an_expired_reservation_replies_instead_of_raising():
    """The D1 dead-letter path: a saga participant must reply with a fact, never
    throw into the relay, or the order is PAID with nobody informed."""
    store, ucs = stocked(10)
    rid = expired_reservation(store, ucs)
    run(ucs["expire"].execute())

    run(ucs["commit_saga"].execute(rid))         # must not raise

    assert store.events("InventoryReservationFailed") == 1
    assert store.events("InventoryCommitted") == 0
    assert store.reservations[rid]["status"] == ReservationStatus.EXPIRED
    assert store.items[(OFFER, LOC)]["on_hand"] == 10   # stock not consumed


def test_commit_for_order_still_commits_a_pending_reservation():
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 4)))

    run(ucs["commit_saga"].execute(rid))

    rec = store.items[(OFFER, LOC)]
    assert (rec["on_hand"], rec["reserved"]) == (6, 0)
    assert store.events("InventoryCommitted") == 1


def test_commit_for_order_is_idempotent_under_redelivery():
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 4)))

    run(ucs["commit_saga"].execute(rid))
    run(ucs["commit_saga"].execute(rid))

    assert store.events("InventoryCommitted") == 1
    assert store.items[(OFFER, LOC)]["on_hand"] == 6


# === saga participant replies ============================================
def test_reserve_for_order_replies_failed_instead_of_raising_on_shortage():
    store, ucs = stocked(2)
    run(ucs["reserve_saga"].execute(
        reservation_id=uuid.uuid4(), offer_id=OFFER, location=LOC, quantity=99))

    assert store.events("InventoryReservationFailed") == 1
    assert store.reserved() == 0


def test_failure_reply_for_an_unknown_offer_persists_its_carrier_row():
    """The outbox stamps aggregate_id from the emitting aggregate; it must resolve
    to a real row, not a phantom created only to carry the event."""
    store = Store()
    ucs = build(store)

    run(ucs["reserve_saga"].execute(
        reservation_id=uuid.uuid4(), offer_id=OFFER, location=LOC, quantity=5))

    assert store.events("InventoryReservationFailed") == 1
    assert (OFFER, LOC) in store.items
    rec = store.items[(OFFER, LOC)]
    assert (rec["on_hand"], rec["reserved"]) == (0, 0)


def test_reserve_for_order_is_idempotent():
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    for _ in range(2):
        run(ucs["reserve_saga"].execute(
            reservation_id=rid, offer_id=OFFER, location=LOC, quantity=3))

    assert store.reserved() == 3
    assert store.events("InventoryReserved") == 1


# === D1 regression: the paid-order-without-stock sequence =================
def test_expired_hold_fails_the_order_before_payment_can_commit_it():
    """The exact ADR-0009 D1 sequence, at the saga's state machine:
    reserved -> TTL lapses -> payment succeeds. The order must NOT reach PAID and
    must NOT request a commit against stock it no longer holds."""
    from src.modules.ordering.domain.entities import (
        LineStatus,
        Order,
        OrderLine,
        OrderStatus,
    )

    line = OrderLine(offer_id=OFFER, location=LOC, quantity=1, unit_amount=1000)
    order = Order.place(
        customer_id=uuid.uuid4(), region="ir", currency="IRR", lines=[line])
    order.pull_events()
    order.on_line_reserved(line.reservation_id)
    assert order.status == OrderStatus.RESERVED
    order.pull_events()

    order.on_line_reservation_expired(line.reservation_id)

    assert order.status == OrderStatus.FAILED
    assert line.status == LineStatus.FAILED
    names = [type(e).__name__ for e in order.pull_events()]
    assert "OrderFailed" in names

    order.on_payment_succeeded()                  # late payment reply

    assert order.status == OrderStatus.FAILED     # never PAID
    assert [type(e).__name__ for e in order.pull_events()] == []


def test_expiry_of_one_line_compensates_its_reserved_siblings():
    from src.modules.ordering.domain.entities import LineStatus, Order, OrderLine

    a = OrderLine(offer_id=OFFER, location=LOC, quantity=1, unit_amount=100)
    b = OrderLine(offer_id=OFFER2, location=LOC, quantity=1, unit_amount=100)
    order = Order.place(
        customer_id=uuid.uuid4(), region="ir", currency="IRR", lines=[a, b])
    order.on_line_reserved(a.reservation_id)
    order.on_line_reserved(b.reservation_id)
    order.pull_events()

    order.on_line_reservation_expired(a.reservation_id)

    names = [type(e).__name__ for e in order.pull_events()]
    assert "InventoryReleaseRequested" in names   # sibling hold handed back
    assert b.status == LineStatus.RELEASED


# === H2: domain exceptions must not escape into the relay ================
def test_commit_for_order_replies_when_counters_cannot_absorb_the_commit():
    """I8 drift: the stock row says less is reserved than the reservation claims.
    Raising here dead-letters a PAID order; the saga must get a fact instead."""
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 4)))
    store.items[(OFFER, LOC)]["reserved"] = 1        # drift under the reservation

    run(ucs["commit_saga"].execute(rid))             # must not raise

    assert store.events("InventoryReservationFailed") == 1
    assert store.events("InventoryCommitted") == 0
    rec = store.items[(OFFER, LOC)]
    assert (rec["on_hand"], rec["reserved"]) == (10, 1)   # counters untouched
    assert store.reservations[rid]["status"] == ReservationStatus.PENDING


def test_sweeper_skips_a_reservation_the_domain_refuses_and_keeps_going():
    """A row that can never expire must not crash the pass, must not half-apply,
    and must not block the younger rows behind it (claims are oldest-first)."""
    store, ucs = stocked(10)
    run(ucs["receive"].execute(
        ReceiveStockCommand(offer_id=str(OFFER2), location=LOC, quantity=10), OPERATOR))

    bad = expired_reservation(store, ucs, qty=4, offer=OFFER)
    store.reservations[bad]["expires_at"] = utcnow() - timedelta(seconds=99)  # claimed first
    store.items[(OFFER, LOC)]["reserved"] = 0        # cannot release 4 of 0
    good = expired_reservation(store, ucs, qty=4, offer=OFFER2)

    swept = run(ucs["expire"].execute())             # must not raise

    assert swept == 1
    assert store.reservations[bad]["status"] == ReservationStatus.PENDING   # not half-applied
    assert store.items[(OFFER, LOC)]["reserved"] == 0
    assert store.reservations[good]["status"] == ReservationStatus.EXPIRED  # not blocked
    assert store.reserved(OFFER2) == 0


def test_sweeper_pass_terminates_when_every_due_row_is_poisoned():
    store, ucs = stocked(10)
    bad = expired_reservation(store, ucs, qty=4)
    store.items[(OFFER, LOC)]["reserved"] = 0

    assert run(ucs["expire"].execute()) == 0
    assert store.reservations[bad]["status"] == ReservationStatus.PENDING


# === domain guards in isolation ==========================================
def test_aggregate_rejects_a_reserve_beyond_available():
    """Pins the guard in the aggregate itself. Without it the oversell is still
    stopped by _check_invariants, so a use-case-level test cannot tell which
    layer fired — this one can."""
    from src.modules.inventory.domain.exceptions import InsufficientStock

    item = InventoryItem.create(offer_id=OFFER, location=LOC)
    item.receive(3)
    with pytest.raises(InsufficientStock):
        item.reserve(4, uuid.uuid4())
    assert item.reserved == 0


def test_aggregate_rejects_expiring_more_than_reserved():
    from src.modules.inventory.domain.exceptions import InvalidReservationState

    item = InventoryItem.create(offer_id=OFFER, location=LOC)
    item.receive(3)
    with pytest.raises(InvalidReservationState):
        item.expire_reservation(1, uuid.uuid4())


# === missing stock row (D6) ==============================================
def test_commit_with_a_missing_stock_row_is_a_conflict_not_a_crash():
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 2)))
    del store.items[(OFFER, LOC)]                 # row vanished under the reservation

    with pytest.raises(ConflictError):
        run(ucs["commit"].execute(rid))


def test_release_with_a_missing_stock_row_is_a_conflict_not_a_crash():
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 2)))
    del store.items[(OFFER, LOC)]

    with pytest.raises(ConflictError):
        run(ucs["release"].execute(rid))


def test_commit_for_order_replies_when_the_stock_row_is_missing():
    """Saga path must still reply with a fact, and the carrier it emits from has
    to be a persisted row so the outbox aggregate_id resolves."""
    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 2)))
    del store.items[(OFFER, LOC)]

    run(ucs["commit_saga"].execute(rid))          # must not raise

    assert store.events("InventoryReservationFailed") == 1
    assert store.events("InventoryCommitted") == 0
    assert (OFFER, LOC) in store.items            # carrier persisted


# === consumer protocol ====================================================
class _Event:
    def __init__(self, payload):
        self.payload = payload


def test_commit_consumer_does_not_raise_on_an_expired_reservation():
    """Guards the consumer's choice of use case. Wired to CommitReservation it
    raises ConflictError into the relay -> 10 retries -> dead letter, and the
    PAID order is never told."""
    from src.modules.inventory.application.consumers import OnInventoryCommitRequested

    store, ucs = stocked(10)
    rid = expired_reservation(store, ucs)
    run(ucs["expire"].execute())

    consumer = OnInventoryCommitRequested(uow_factory=lambda: FakeInvUoW(store))
    run(consumer.handle(None, _Event({"reservation_ids": [str(rid)]})))

    assert store.events("InventoryReservationFailed") == 1
    assert store.events("InventoryCommitted") == 0


def test_commit_consumer_still_commits_pending_reservations():
    from src.modules.inventory.application.consumers import OnInventoryCommitRequested

    store, ucs = stocked(10)
    rid = uuid.uuid4()
    run(ucs["reserve"].execute(reserve_cmd(rid, 3)))

    consumer = OnInventoryCommitRequested(uow_factory=lambda: FakeInvUoW(store))
    run(consumer.handle(None, _Event({"reservation_ids": [str(rid)]})))

    assert store.events("InventoryCommitted") == 1
    assert store.items[(OFFER, LOC)]["on_hand"] == 7


# === SQL adapter (statement-level, still DB-free) =========================
class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Scalars:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return self._rows


class CapturingSession:
    """Enough AsyncSession surface to compile and inspect the emitted SQL."""

    def __init__(self, rowcount: int = 1, rows=()) -> None:
        self.statements: list = []
        self._rowcount = rowcount
        self._rows = list(rows)

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self._rowcount)

    async def scalars(self, stmt):
        self.statements.append(stmt)
        return _Scalars(self._rows)


def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def _sql_repo(session):
    from src.modules.inventory.infrastructure.repository import SqlInventoryRepository

    return SqlInventoryRepository(session)


def _an_item() -> InventoryItem:
    item = InventoryItem(
        id=uuid.uuid4(), offer_id=OFFER, location=LOC,
        on_hand=10, reserved=2, version=7,
    )
    return item


def test_sql_save_item_carries_the_version_predicate():
    """The real oversell guard under concurrency. The in-memory fake reimplements
    it, so only this test pins the actual UPDATE."""
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rowcount=1)
    run(_sql_repo(session).save_item(_an_item()))

    sql = _sql(session.statements[0])
    assert "version = " in sql.split("WHERE", 1)[1]


def test_sql_save_item_translates_a_lost_race_into_a_conflict():
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rowcount=0)          # someone else moved the row
    with pytest.raises(ConcurrencyConflictError):
        run(_sql_repo(session).save_item(_an_item()))


def test_sql_save_item_bumps_the_version():
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rowcount=1)
    item = _an_item()
    run(_sql_repo(session).save_item(item))
    assert item.version == 8


def test_sql_save_reservation_carries_the_status_predicate():
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rowcount=1)
    res = Reservation(id=uuid.uuid4(), offer_id=OFFER, location=LOC, quantity=1,
                      status=ReservationStatus.COMMITTED)

    run(_sql_repo(session).save_reservation(res, expected_status=ReservationStatus.PENDING))

    assert "status = " in _sql(session.statements[0]).split("WHERE", 1)[1]


def test_sql_save_reservation_translates_a_status_race_into_a_conflict():
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rowcount=0)
    res = Reservation(id=uuid.uuid4(), offer_id=OFFER, location=LOC, quantity=1,
                      status=ReservationStatus.COMMITTED)

    with pytest.raises(ConcurrencyConflictError):
        run(_sql_repo(session).save_reservation(res, expected_status=ReservationStatus.PENDING))


def test_sql_expiry_claim_uses_for_update_skip_locked():
    """Parallel sweepers must claim disjoint rows rather than block each other."""
    pytest.importorskip("sqlalchemy")
    session = CapturingSession(rows=[])
    run(_sql_repo(session).find_expired_pending(utcnow(), limit=1))

    sql = _sql(session.statements[0]).upper()
    assert "FOR UPDATE" in sql and "SKIP LOCKED" in sql


# === wiring ===============================================================
def test_inventory_expired_has_a_registered_consumer():
    """Without this subscription the expiry event is emitted into the void."""
    pytest.importorskip("sqlalchemy", reason="relay wiring pulls in the DB layer")
    from src.shared_kernel.infrastructure.outbox.runner import build_registry

    handlers = build_registry().handlers_for("inventory.InventoryExpired")
    assert [h.name for h in handlers] == ["ordering.on_inventory_expired"]


def test_reservation_status_constants_match_the_db_check_constraint():
    """ck_resv_status is hand-written SQL; a new status added in Python only is
    a runtime IntegrityError, not a test failure, without this."""
    pytest.importorskip("sqlalchemy", reason="ORM model pulls in the DB layer")
    from src.modules.inventory.infrastructure.models import ReservationModel

    check = next(
        c for c in ReservationModel.__table__.constraints
        if getattr(c, "name", None) == "ck_resv_status"
    )
    allowed = {v.strip().strip("'") for v in
               str(check.sqltext).split("(")[1].rstrip(")").split(",")}
    assert allowed == set(ReservationStatus.ALL)
