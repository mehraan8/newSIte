"""Ordering saga validation (DB-free) — state machine + compensation + idempotency.

Exercises the real Order process-manager aggregate and asserts the exact events
it records as participant replies arrive. PII guard runs on every event.

Run: python tests/test_ordering_saga.py
"""

from __future__ import annotations

import sys
import uuid

sys.path.insert(0, ".")

from src.modules.ordering.domain.entities import (
    InventoryCommitRequested,
    InventoryReleaseRequested,
    OrderFailed,
    OrderLine,
    OrderPaid,
    OrderPlaced,
    OrderReservationRequested,
    OrderReserved,
    OrderStatus,
    Order,
    PaymentRequested,
)
from src.shared_kernel.infrastructure.outbox.serialization import event_payload


def drain(order):
    evs = order.pull_events()
    for e in evs:
        event_payload(e)  # PII guard, as the UoW/outbox would
    return evs


def names(evs):
    return [type(e).__name__ for e in evs]


def two_line_order():
    return Order.place(
        customer_id=uuid.uuid4(), region="ir", currency="IRR",
        lines=[
            OrderLine(offer_id=uuid.uuid4(), location="default", quantity=2, unit_amount=100),
            OrderLine(offer_id=uuid.uuid4(), location="default", quantity=1, unit_amount=50),
        ],
    )


def run():
    # === placement: OrderPlaced + reserve command ===
    o = two_line_order()
    evs = drain(o)
    assert names(evs) == ["OrderPlaced", "OrderReservationRequested"], names(evs)
    placed = next(e for e in evs if isinstance(e, OrderPlaced))
    assert placed.total_amount == 2 * 100 + 1 * 50 and placed.line_count == 2
    req = next(e for e in evs if isinstance(e, OrderReservationRequested))
    rids = [uuid.UUID(ln["reservation_id"]) for ln in req.lines]
    assert o.status == OrderStatus.PENDING and len(rids) == 2
    print("[place]    OrderPlaced + OrderReservationRequested; status pending")

    # === happy path: both reserved -> RESERVED + PaymentRequested ===
    o.on_line_reserved(rids[0])
    assert names(drain(o)) == []                    # first line: no order-level event yet
    o.on_line_reserved(rids[1])
    evs = drain(o)
    assert names(evs) == ["OrderReserved", "PaymentRequested"], names(evs)
    assert o.status == OrderStatus.RESERVED
    pay = next(e for e in evs if isinstance(e, PaymentRequested))
    assert pay.amount == 250 and pay.currency == "IRR"
    print("[reserve]  all lines reserved -> RESERVED + PaymentRequested(250)")

    # === payment success -> PAID + commit command ===
    o.on_payment_succeeded()
    evs = drain(o)
    assert names(evs) == ["OrderPaid", "InventoryCommitRequested"], names(evs)
    commit = next(e for e in evs if isinstance(e, InventoryCommitRequested))
    assert set(commit.reservation_ids) == {str(r) for r in rids}
    assert o.status == OrderStatus.PAID
    print("[paid]     OrderPaid + InventoryCommitRequested(both)")

    # idempotency: replaying reserved/paid does nothing
    o.on_line_reserved(rids[0]); o.on_payment_succeeded()
    assert names(drain(o)) == []
    print("[idem]     replayed reserve/payment -> no events")

    # === COMPENSATION: partial reservation failure ===
    o2 = two_line_order()
    req2 = next(e for e in drain(o2) if isinstance(e, OrderReservationRequested))
    r2 = [uuid.UUID(ln["reservation_id"]) for ln in req2.lines]
    o2.on_line_reserved(r2[0])          # line 0 reserved
    drain(o2)
    o2.on_line_reservation_failed(r2[1])  # line 1 fails
    evs = drain(o2)
    assert names(evs) == ["OrderFailed", "InventoryReleaseRequested"], names(evs)
    rel = next(e for e in evs if isinstance(e, InventoryReleaseRequested))
    assert set(rel.reservation_ids) == {str(r2[0])}    # only the reserved line is released
    assert o2.status == OrderStatus.FAILED
    print("[comp-1]   reservation failure -> OrderFailed + release the reserved line")

    # === COMPENSATION: payment failure after full reservation ===
    o3 = two_line_order()
    req3 = next(e for e in drain(o3) if isinstance(e, OrderReservationRequested))
    r3 = [uuid.UUID(ln["reservation_id"]) for ln in req3.lines]
    o3.on_line_reserved(r3[0]); o3.on_line_reserved(r3[1]); drain(o3)
    assert o3.status == OrderStatus.RESERVED
    o3.on_payment_failed()
    evs = drain(o3)
    assert names(evs) == ["OrderFailed", "InventoryReleaseRequested"], names(evs)
    rel3 = next(e for e in evs if isinstance(e, InventoryReleaseRequested))
    assert set(rel3.reservation_ids) == {str(r3[0]), str(r3[1])}   # release both
    print("[comp-2]   payment failure -> OrderFailed + release all reserved lines")

    # === idempotent failure: further replies after FAILED do nothing ===
    o3.on_line_reserved(r3[0]); o3.on_payment_failed()
    assert names(drain(o3)) == []
    print("[idem-2]   replies after FAILED are ignored")

    # === cancel a reserved order -> compensation ===
    o4 = two_line_order()
    req4 = next(e for e in drain(o4) if isinstance(e, OrderReservationRequested))
    r4 = [uuid.UUID(ln["reservation_id"]) for ln in req4.lines]
    o4.on_line_reserved(r4[0]); o4.on_line_reserved(r4[1]); drain(o4)
    o4.cancel()
    evs = drain(o4)
    assert "OrderCancelled" in names(evs) and "InventoryReleaseRequested" in names(evs)
    assert o4.status == OrderStatus.CANCELLED
    print("[cancel]   cancel reserved order -> release + OrderCancelled")

    print("\nORDERING SAGA VALIDATED (state machine + happy path + compensation + idempotency)")


if __name__ == "__main__":
    run()
