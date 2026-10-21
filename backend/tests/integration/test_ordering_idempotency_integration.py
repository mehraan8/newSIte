"""Checkout idempotency against live Postgres.

The unit tests reimplement the dedup in a fake. Only this proves the composite
primary key actually rejects a duplicate, that the key row commits atomically
with the order, and that a rolled-back attempt leaves neither behind.
Requires TEST_DATABASE_URL; skipped otherwise.
"""

from __future__ import annotations

import uuid

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


def pricing():
    from src.modules.ordering.infrastructure.buybox_acl import SqlBuyBoxPricingGateway

    return SqlBuyBoxPricingGateway(session_factory=db.session_factory())


def add_buybox_offer(offer_id, *, region="ir", amount=PRICE):
    from datetime import datetime, timezone

    db.execute(
        "insert into buy_box_offer "
        "(offer_id, variant_id, seller_id, region, currency, amount, scale, "
        " is_active, offer_created_at) "
        "values (:offer, :variant, :seller, :region, 'IRR', :amount, 0, true, :created)",
        offer=offer_id, variant=uuid.uuid4(), seller=uuid.uuid4(), region=region,
        amount=amount, created=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def checkout(customer, offer, *, key, quantity=1):
    from src.modules.ordering.application.dtos import OrderLineInput, PlaceOrderCommand
    from src.modules.ordering.application.use_cases import PlaceOrder
    from src.shared_kernel.application.event_context import system_event_context

    cmd = PlaceOrderCommand(
        customer_id=str(customer), region="ir", currency="IRR",
        lines=(OrderLineInput(offer_id=str(offer), location=LOC, quantity=quantity),),
        idempotency_key=key,
    )
    with system_event_context(region="ir"):
        return db.run(PlaceOrder(ord_uow, pricing()).execute(cmd))


def order_count() -> int:
    return int(db.scalar("select count(*) from orders_saga"))


def key_count() -> int:
    return int(db.scalar("select count(*) from order_idempotency"))


def test_a_retried_checkout_creates_exactly_one_order(offer_id):
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)

    first = checkout(customer, offer_id, key="k-1")
    second = checkout(customer, offer_id, key="k-1")

    assert first.id == second.id
    assert order_count() == 1
    assert key_count() == 1


def test_the_key_row_commits_with_the_order(offer_id):
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)

    view = checkout(customer, offer_id, key="k-2")

    stored = db.scalar(
        "select order_id from order_idempotency "
        "where customer_id = :c and idempotency_key = 'k-2'", c=customer)
    assert str(stored) == view.id


def test_a_retry_emits_no_second_reservation_command(offer_id):
    """The real harm of a duplicate: a second OrderReservationRequested holds
    the same stock twice."""
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)

    checkout(customer, offer_id, key="k-3")
    checkout(customer, offer_id, key="k-3")

    emitted = int(db.scalar(
        "select count(*) from outbox_messages "
        "where event_type = 'ordering.OrderReservationRequested'"))
    assert emitted == 1


def test_the_composite_primary_key_rejects_a_duplicate(offer_id):
    """The backstop under the check-then-insert, proven by the database."""
    import sqlalchemy.exc

    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    view = checkout(customer, offer_id, key="k-4")

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.execute(
            "insert into order_idempotency "
            "(customer_id, idempotency_key, order_id, request_fingerprint) "
            "values (:c, 'k-4', :o, 'other')", c=customer, o=uuid.UUID(view.id))


def test_keys_are_scoped_per_customer(offer_id):
    add_buybox_offer(offer_id)
    a = checkout(uuid.uuid4(), offer_id, key="shared")
    b = checkout(uuid.uuid4(), offer_id, key="shared")

    assert a.id != b.id
    assert order_count() == 2


def test_reusing_a_key_with_a_different_body_is_rejected(offer_id):
    from src.shared_kernel.application.errors import ConflictError

    customer = uuid.uuid4()
    add_buybox_offer(offer_id)
    checkout(customer, offer_id, key="k-5", quantity=1)

    with pytest.raises(ConflictError):
        checkout(customer, offer_id, key="k-5", quantity=7)

    assert order_count() == 1


def test_a_rejected_checkout_leaves_no_order_and_no_key(offer_id):
    """Pricing fails after the key was chosen — neither row may survive."""
    from src.shared_kernel.application.errors import NotFoundError

    customer = uuid.uuid4()          # no buy_box_offer row at all

    with pytest.raises(NotFoundError):
        checkout(customer, offer_id, key="k-6")

    assert order_count() == 0
    assert key_count() == 0


def test_without_a_key_duplicates_are_still_created(offer_id):
    """Opt-in by design: documents the gap until the header is made mandatory."""
    customer = uuid.uuid4()
    add_buybox_offer(offer_id)

    a = checkout(customer, offer_id, key=None)
    b = checkout(customer, offer_id, key=None)

    assert a.id != b.id
    assert order_count() == 2
    assert key_count() == 0
