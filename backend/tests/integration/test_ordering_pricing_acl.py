"""Ordering's pricing ACL against live Postgres.

The unit tests use a fake gateway, so nothing there proves the raw SQL reads the
right table, columns or region. Requires TEST_DATABASE_URL; skipped otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.integration import db

db.skip_if_unavailable()  # module-level: an unrun suite must never look green

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def gateway():
    from src.modules.ordering.infrastructure.buybox_acl import SqlBuyBoxPricingGateway

    return SqlBuyBoxPricingGateway(session_factory=db.session_factory())


def price_for(offer_id, region="ir"):
    return db.run(gateway().price_for(offer_id, region))


def add_buybox_offer(offer_id, *, region="ir", currency="IRR", amount=1_000_000,
                     scale=0, is_active=True):
    db.execute(
        "insert into buy_box_offer "
        "(offer_id, variant_id, seller_id, region, currency, amount, scale, "
        " is_active, offer_created_at) "
        "values (:offer, :variant, :seller, :region, :currency, :amount, :scale, "
        "        :active, :created)",
        offer=offer_id, variant=uuid.uuid4(), seller=uuid.uuid4(), region=region,
        currency=currency, amount=amount, scale=scale, active=is_active, created=T0,
    )


def test_an_active_priced_offer_resolves(offer_id):
    add_buybox_offer(offer_id, amount=1_000_000)

    snap = price_for(offer_id)

    assert snap is not None
    assert snap.amount == 1_000_000
    assert snap.currency == "IRR"
    assert snap.scale == 0
    assert snap.is_active is True


def test_an_unknown_offer_resolves_to_none(offer_id):
    assert price_for(uuid.uuid4()) is None


def test_an_offer_in_another_region_is_not_visible(offer_id):
    """Region scoping happens in SQL — a price from the wrong market must never
    be usable to order in this one."""
    add_buybox_offer(offer_id, region="eu")

    assert price_for(offer_id, "ir") is None
    assert price_for(offer_id, "eu") is not None


def test_an_inactive_offer_is_returned_but_flagged(offer_id):
    """The ACL stays a dumb reader; sellability policy lives in the use case."""
    add_buybox_offer(offer_id, is_active=False)

    snap = price_for(offer_id)

    assert snap is not None
    assert snap.is_active is False


def test_an_unpriced_offer_returns_a_null_amount(offer_id):
    add_buybox_offer(offer_id, amount=None, currency=None)

    snap = price_for(offer_id)

    assert snap is not None
    assert snap.amount is None


def test_scale_is_read_back_faithfully(offer_id):
    add_buybox_offer(offer_id, currency="USD", amount=1999, scale=2)

    snap = price_for(offer_id)

    assert (snap.currency, snap.amount, snap.scale) == ("USD", 1999, 2)


def test_place_order_prices_from_the_real_read_model(offer_id):
    """End-to-end: the production gateway, the production use case, real rows —
    the caller supplies only offer/quantity and gets the read model's price."""
    from src.modules.ordering.application.dtos import OrderLineInput, PlaceOrderCommand
    from src.modules.ordering.application.use_cases import PlaceOrder
    from src.modules.ordering.infrastructure.unit_of_work import (
        SqlAlchemyOrderingUnitOfWork,
    )
    from src.shared_kernel.application.event_context import system_event_context

    add_buybox_offer(offer_id, amount=750_000)

    def ord_uow():
        return SqlAlchemyOrderingUnitOfWork(session_factory=db.session_factory())

    with system_event_context(region="ir"):
        view = db.run(PlaceOrder(ord_uow, gateway()).execute(PlaceOrderCommand(
            customer_id=str(uuid.uuid4()), region="ir", currency="IRR",
            lines=(OrderLineInput(offer_id=str(offer_id), location="default", quantity=2),),
        )))

    assert view.lines[0].unit_amount == 750_000
    assert view.total_amount == 1_500_000

    persisted = db.scalar(
        "select unit_amount from order_saga_lines where order_id = :oid", oid=view.id
    )
    assert int(persisted) == 750_000
