"""I8 reconciliation against live Postgres.

FULL OUTER JOIN, the grouped subquery and NULL/COALESCE handling are real
database semantics — a fake cannot prove this query finds drift. Requires
TEST_DATABASE_URL; skipped otherwise.
"""

from __future__ import annotations

import uuid

import pytest

from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal
from tests.integration import db

db.skip_if_unavailable()  # module-level: an unrun suite must never look green

pytestmark = pytest.mark.integration


OPERATOR = Principal(
    subject_id=uuid.uuid4(),
    platform_permissions=frozenset({
        Permission.INVENTORY_MANAGE_ANY,
        Permission.INVENTORY_RESERVATIONS_CREATE,
        Permission.INVENTORY_RESERVATIONS_COMMIT,
        Permission.INVENTORY_RESERVATIONS_RELEASE,
    }),
)


def find_drift():
    from src.modules.inventory.infrastructure.reconciliation import find_drift

    return db.run(find_drift(db.session_factory()))


def add_stock(offer_id, *, on_hand, reserved, location="default"):
    db.execute(
        "insert into inventory_stock (id, offer_id, location, on_hand, reserved, version) "
        "values (:id, :offer, :loc, :on_hand, :reserved, 0)",
        id=uuid.uuid4(), offer=offer_id, loc=location,
        on_hand=on_hand, reserved=reserved,
    )


def add_reservation(offer_id, *, quantity, status="pending", location="default"):
    rid = uuid.uuid4()
    db.execute(
        "insert into inventory_reservation "
        "(id, offer_id, location, quantity, status) "
        "values (:id, :offer, :loc, :qty, :status)",
        id=rid, offer=offer_id, loc=location, qty=quantity, status=status,
    )
    return rid


def test_consistent_state_reports_no_drift(offer_id):
    add_stock(offer_id, on_hand=10, reserved=3)
    add_reservation(offer_id, quantity=3)

    assert find_drift() == []


def test_multiple_pending_holds_sum_against_the_counter(offer_id):
    add_stock(offer_id, on_hand=10, reserved=6)
    add_reservation(offer_id, quantity=4)
    add_reservation(offer_id, quantity=2)

    assert find_drift() == []


def test_non_pending_reservations_are_excluded_from_the_sum(offer_id):
    """Committed/released/expired holds are settled and must not count."""
    add_stock(offer_id, on_hand=10, reserved=2)
    add_reservation(offer_id, quantity=2)
    add_reservation(offer_id, quantity=7, status="committed")
    add_reservation(offer_id, quantity=5, status="released")
    add_reservation(offer_id, quantity=3, status="expired")

    assert find_drift() == []


def test_counter_below_live_holds_is_detected_as_oversellable(offer_id):
    add_stock(offer_id, on_hand=10, reserved=1)
    add_reservation(offer_id, quantity=4)

    drift = find_drift()

    assert len(drift) == 1
    assert drift[0].reserved_counter == 1
    assert drift[0].pending_total == 4
    assert drift[0].oversellable is True


def test_counter_above_live_holds_is_detected_as_stranded(offer_id):
    add_stock(offer_id, on_hand=10, reserved=6)
    add_reservation(offer_id, quantity=2)

    drift = find_drift()

    assert len(drift) == 1
    assert drift[0].delta == 4
    assert drift[0].oversellable is False


def test_pending_holds_with_no_stock_row_are_detected(offer_id):
    """The FULL OUTER JOIN's right side — a plain inner join would miss this
    entirely, and it is the worst case: holds against stock that does not exist."""
    add_reservation(offer_id, quantity=3)

    drift = find_drift()

    assert len(drift) == 1
    assert drift[0].reserved_counter == 0
    assert drift[0].pending_total == 3
    assert drift[0].oversellable is True


def test_locations_are_reconciled_independently(offer_id):
    add_stock(offer_id, on_hand=5, reserved=2, location="tehran")
    add_reservation(offer_id, quantity=2, location="tehran")
    add_stock(offer_id, on_hand=5, reserved=0, location="mashhad")
    add_reservation(offer_id, quantity=1, location="mashhad")

    drift = find_drift()

    assert [d.location for d in drift] == ["mashhad"]


def test_offers_are_reconciled_independently(offer_id, second_offer_id):
    add_stock(offer_id, on_hand=5, reserved=1)
    add_reservation(offer_id, quantity=1)
    add_stock(second_offer_id, on_hand=5, reserved=4)
    add_reservation(second_offer_id, quantity=1)

    drift = find_drift()

    assert len(drift) == 1
    assert drift[0].offer_id == str(second_offer_id)


def test_a_real_reserve_leaves_no_drift(offer_id):
    """End-to-end: the use case's own writes must satisfy I8. This is the check
    that would catch a future change breaking the two-row write."""
    from src.modules.inventory.application.dtos import ReserveStockCommand
    from src.modules.inventory.application.use_cases import ReserveStock
    from src.modules.inventory.infrastructure.unit_of_work import (
        SqlAlchemyInventoryUnitOfWork,
    )
    from src.shared_kernel.application.event_context import system_event_context

    def inv_uow():
        return SqlAlchemyInventoryUnitOfWork(session_factory=db.session_factory())

    add_stock(offer_id, on_hand=10, reserved=0)
    with system_event_context():
        db.run(ReserveStock(inv_uow).execute(ReserveStockCommand(
            reservation_id=str(uuid.uuid4()), offer_id=str(offer_id),
            location="default", quantity=4,
        ), OPERATOR))

    assert find_drift() == []
