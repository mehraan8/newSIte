"""Cancel path and persisted total against live Postgres.

Proves the cancel write survives the version CAS, that CANCELLED satisfies
ck_order_status, and that the stored total_amount column tracks the aggregate.
Requires TEST_DATABASE_URL; skipped otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.integration import db

db.skip_if_unavailable()  # module-level: an unrun suite must never look green

pytestmark = pytest.mark.integration

LOC = "default"
PRICE = 750_000


def ord_uow():
    from src.modules.ordering.infrastructure.unit_of_work import (
        SqlAlchemyOrderingUnitOfWork,
    )

    return SqlAlchemyOrderingUnitOfWork(session_factory=db.session_factory())


def add_buybox_offer(offer_id, *, amount=PRICE, region="ir"):
    db.execute(
        "insert into buy_box_offer "
        "(offer_id, variant_id, seller_id, region, currency, amount, scale, "
        " is_active, offer_created_at) "
        "values (:offer, :variant, :seller, :region, 'IRR', :amount, 0, true, :created)",
        offer=offer_id, variant=uuid.uuid4(), seller=uuid.uuid4(), region=region,
        amount=amount, created=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def checkout(customer, offer, *, quantity=1):
    from src.modules.ordering.application.dtos import OrderLineInput, PlaceOrderCommand
    from src.modules.ordering.application.use_cases import PlaceOrder
    from src.modules.ordering.infrastructure.buybox_acl import SqlBuyBoxPricingGateway
    from src.shared_kernel.application.event_context import system_event_context

    cmd = PlaceOrderCommand(
        customer_id=str(customer), region="ir", currency="IRR",
        lines=(OrderLineInput(offer_id=str(offer), location=LOC, quantity=quantity),),
    )
    with system_event_context(region="ir"):
        return db.run(PlaceOrder(
            ord_uow, SqlBuyBoxPricingGateway(session_factory=db.session_factory())
        ).execute(cmd))


def cancel(order_id, customer):
    from src.modules.ordering.application.use_cases import CancelOrder
    from src.shared_kernel.application.event_context import system_event_context

    with system_event_context(region="ir"):
        return db.run(CancelOrder(ord_uow).execute(
            uuid.UUID(order_id), customer_id=customer))


def stored(order_id, column):
    return db.scalar(f"select {column} from orders_saga where id = :id", id=order_id)


def test_cancelling_a_pending_order_persists_and_bumps_the_version(offer_id):
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id)
    assert stored(view.id, "version") == 0

    cancel(view.id, customer)

    assert stored(view.id, "status") == "cancelled"
    assert stored(view.id, "version") == 1


def test_cancelled_satisfies_the_status_check_constraint(offer_id):
    """ck_order_status is hand-written SQL; a status the domain allows but the
    constraint rejects would only surface here."""
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id)

    cancel(view.id, customer)          # would raise IntegrityError if rejected

    assert stored(view.id, "status") == "cancelled"


def test_cancel_emits_ordercancelled_to_the_outbox(offer_id):
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id)

    cancel(view.id, customer)

    emitted = int(db.scalar(
        "select count(*) from outbox_messages "
        "where event_type = 'ordering.OrderCancelled' and aggregate_id = :id",
        id=view.id))
    assert emitted == 1


def test_cancelling_twice_emits_one_event(offer_id):
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id)

    cancel(view.id, customer)
    cancel(view.id, customer)

    emitted = int(db.scalar(
        "select count(*) from outbox_messages "
        "where event_type = 'ordering.OrderCancelled' and aggregate_id = :id",
        id=view.id))
    assert emitted == 1


def test_another_customer_cannot_cancel(offer_id):
    from src.shared_kernel.application.errors import ForbiddenError

    owner = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(owner, offer_id)

    with pytest.raises(ForbiddenError):
        cancel(view.id, uuid.uuid4())

    assert stored(view.id, "status") == "pending"


def test_a_cancelled_order_releases_its_reserved_lines(offer_id):
    from src.modules.ordering.application.use_cases import HandleInventoryReserved
    from src.shared_kernel.application.event_context import system_event_context

    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id)
    rid = uuid.UUID(view.lines[0].reservation_id)

    with system_event_context(region="ir"):
        db.run(HandleInventoryReserved(ord_uow).execute(rid))
    cancel(view.id, customer)

    assert db.scalar(
        "select status from order_saga_lines where reservation_id = :r", r=rid
    ) == "released"
    released = int(db.scalar(
        "select count(*) from outbox_messages "
        "where event_type = 'ordering.InventoryReleaseRequested' "
        "and payload::text like :rid", rid=f"%{rid}%"))
    assert released == 1


# === total_amount ==========================================================
def test_the_stored_total_matches_the_lines_on_insert(offer_id):
    customer = uuid.uuid4()
    add_buybox_offer(offer_id, amount=PRICE)

    view = checkout(customer, offer_id, quantity=3)

    assert int(stored(view.id, "total_amount")) == PRICE * 3
    assert view.total_amount == PRICE * 3


def test_a_stale_total_is_repaired_by_the_next_save(offer_id):
    """The O8 fix, proven against the column: corrupt it, then make the saga
    write, and the value must be re-materialised from the lines."""
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id, quantity=2)

    db.execute("update orders_saga set total_amount = 1 where id = :id", id=view.id)
    assert int(stored(view.id, "total_amount")) == 1

    cancel(view.id, customer)

    assert int(stored(view.id, "total_amount")) == PRICE * 2


def test_the_total_is_unchanged_by_saga_progress(offer_id):
    from src.modules.ordering.application.use_cases import HandleInventoryReserved
    from src.shared_kernel.application.event_context import system_event_context

    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id, quantity=2)

    with system_event_context(region="ir"):
        db.run(HandleInventoryReserved(ord_uow).execute(
            uuid.UUID(view.lines[0].reservation_id)))

    assert int(stored(view.id, "total_amount")) == PRICE * 2
