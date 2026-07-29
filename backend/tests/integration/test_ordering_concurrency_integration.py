"""Ordering's optimistic lock against live Postgres.

The unit tests reimplement the version guard in a fake. Only this proves the real
UPDATE carries the predicate, that rowcount 0 becomes a conflict, and that two
genuinely concurrent transactions cannot both win. Requires TEST_DATABASE_URL.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration import db

db.skip_if_unavailable()  # module-level: an unrun suite must never look green

pytestmark = pytest.mark.integration

LOC = "default"


def ord_uow():
    from src.modules.ordering.infrastructure.unit_of_work import (
        SqlAlchemyOrderingUnitOfWork,
    )

    return SqlAlchemyOrderingUnitOfWork(session_factory=db.session_factory())


def place(offer_a, offer_b=None):
    """Creates a real order through the repository, bypassing pricing."""
    from src.modules.ordering.domain.entities import Order, OrderLine
    from src.shared_kernel.application.event_context import system_event_context

    lines = [OrderLine(offer_id=offer_a, location=LOC, quantity=1, unit_amount=100)]
    if offer_b is not None:
        lines.append(OrderLine(offer_id=offer_b, location=LOC, quantity=1, unit_amount=150))

    async def go():
        uow = ord_uow()
        async with uow:
            order = Order.place(
                customer_id=uuid.uuid4(), region="ir", currency="IRR", lines=lines)
            await uow.orders.add(order)
            await uow.commit()
            return order

    with system_event_context(region="ir"):
        return db.run(go())


def stored_version(order_id) -> int:
    return int(db.scalar("select version from orders_saga where id = :id", id=order_id))


def line_statuses(order_id) -> list[str]:
    return [
        r.status for r in db.rows(
            "select status from order_saga_lines where order_id = :id "
            "order by reservation_id", id=order_id)
    ]


def test_a_new_order_persists_with_version_zero(offer_id):
    order = place(offer_id)
    assert stored_version(order.id) == 0


def test_a_save_bumps_the_stored_version(offer_id):
    from src.modules.ordering.application.use_cases import HandleInventoryReserved
    from src.shared_kernel.application.event_context import system_event_context

    order = place(offer_id)
    with system_event_context(region="ir"):
        db.run(HandleInventoryReserved(ord_uow).execute(order.lines[0].reservation_id))

    assert stored_version(order.id) == 1


def test_two_stale_writers_cannot_both_commit(offer_id, second_offer_id):
    """The lost update, for real: both transactions read version 0, both mutate a
    different line, only one may land."""
    from src.shared_kernel.application.errors import ConcurrencyConflictError
    from src.shared_kernel.application.event_context import system_event_context

    order = place(offer_id, second_offer_id)
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id

    async def go():
        uow_a, uow_b = ord_uow(), ord_uow()
        async with uow_a:
            async with uow_b:
                a = await uow_a.orders.find_by_reservation_id(rid_a)
                b = await uow_b.orders.find_by_reservation_id(rid_b)   # same version
                assert a.version == b.version == 0

                a.on_line_reserved(rid_a)
                await uow_a.orders.save(a)
                await uow_a.commit()

                b.on_line_reserved(rid_b)
                with pytest.raises(ConcurrencyConflictError):
                    await uow_b.orders.save(b)

    with system_event_context(region="ir"):
        db.run(go())

    assert stored_version(order.id) == 1
    assert sorted(line_statuses(order.id)) == ["pending", "reserved"]


def test_the_retrying_handler_lands_both_lines(offer_id, second_offer_id):
    """Same race, but through the production handlers — the loser retries
    against fresh state and both line statuses survive."""
    from src.modules.ordering.application.use_cases import HandleInventoryReserved
    from src.shared_kernel.application.event_context import system_event_context

    order = place(offer_id, second_offer_id)
    handler = HandleInventoryReserved(ord_uow)

    with system_event_context(region="ir"):
        db.run(handler.execute(order.lines[0].reservation_id))
        db.run(handler.execute(order.lines[1].reservation_id))

    assert line_statuses(order.id) == ["reserved", "reserved"]
    assert db.scalar("select status from orders_saga where id = :id", id=order.id) == "reserved"


def test_late_reserve_on_a_failed_order_persists_as_released(offer_id, second_offer_id):
    """End-to-end leak check against real rows: no line may be left 'reserved'
    on a failed order."""
    from src.modules.ordering.application.use_cases import (
        HandleInventoryReservationFailed,
        HandleInventoryReserved,
    )
    from src.shared_kernel.application.event_context import system_event_context

    order = place(offer_id, second_offer_id)
    rid_a, rid_b = order.lines[0].reservation_id, order.lines[1].reservation_id

    with system_event_context(region="ir"):
        db.run(HandleInventoryReservationFailed(ord_uow).execute(rid_a))
        db.run(HandleInventoryReserved(ord_uow).execute(rid_b))

    assert db.scalar("select status from orders_saga where id = :id", id=order.id) == "failed"
    assert "reserved" not in line_statuses(order.id)

    released = db.scalar(
        "select count(*) from outbox_messages "
        "where event_type = 'ordering.InventoryReleaseRequested' "
        "and payload::text like :rid", rid=f"%{rid_b}%")
    assert int(released) == 1
