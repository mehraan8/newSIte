"""Live Inventory validation against real Postgres and the real relay.

Everything here runs the production objects: SqlAlchemyInventoryUnitOfWork,
SqlInventoryRepository, SqlSellingGateway, OutboxRelayWorker, EventDispatcher,
SqlInboxStore, and the real Alembic schema. No fake repositories.

Requires a scratch Postgres. Set TEST_DATABASE_URL and re-run; the suite SKIPS
(never passes) when the database is unreachable.

    pytest tests/integration -m integration
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.integration import db

db.skip_if_unavailable()  # module-level: an unrun suite must never look green

from src.modules.inventory.application.dtos import (  # noqa: E402
    ReceiveStockCommand,
    ReserveStockCommand,
)
from src.modules.inventory.application.use_cases import (  # noqa: E402
    CommitForOrder,
    CommitReservation,
    ExpireReservations,
    ReceiveStock,
    ReleaseReservation,
    ReserveForOrder,
    ReserveStock,
)
from src.modules.inventory.domain.entities import ReservationStatus  # noqa: E402
from src.modules.inventory.infrastructure.selling_acl import SqlSellingGateway  # noqa: E402
from src.modules.inventory.infrastructure.unit_of_work import (  # noqa: E402
    SqlAlchemyInventoryUnitOfWork,
)
from src.modules.ordering.application.dtos import (  # noqa: E402
    OrderLineInput,
    PlaceOrderCommand,
)
from src.modules.ordering.application.use_cases import PlaceOrder  # noqa: E402
from src.modules.ordering.infrastructure.unit_of_work import (  # noqa: E402
    SqlAlchemyOrderingUnitOfWork,
)
from src.shared_kernel.application.errors import (  # noqa: E402
    ConcurrencyConflictError,
    ConflictError,
)
from src.shared_kernel.application.event_context import system_event_context  # noqa: E402
from src.shared_kernel.domain.base import utcnow  # noqa: E402
from src.shared_kernel.infrastructure.outbox.dispatcher import (  # noqa: E402
    EventDispatcher,
    InProcessPublisher,
)
from src.shared_kernel.infrastructure.outbox.relay import OutboxRelayWorker  # noqa: E402
from src.shared_kernel.infrastructure.outbox.runner import build_registry  # noqa: E402
from src.shared_kernel.infrastructure.outbox.stores import (  # noqa: E402
    SqlDeadLetterStore,
    SqlInboxStore,
)

pytestmark = pytest.mark.integration

LOC = "default"
run = db.run


# --- production objects bound to the test engine ---------------------------
def inv_uow():
    return SqlAlchemyInventoryUnitOfWork(session_factory=db.session_factory())


def ord_uow():
    return SqlAlchemyOrderingUnitOfWork(session_factory=db.session_factory())


def pricing():
    from src.modules.ordering.infrastructure.buybox_acl import SqlBuyBoxPricingGateway

    return SqlBuyBoxPricingGateway(session_factory=db.session_factory())


def add_buybox_offer(offer_id: uuid.UUID, *, region: str = "ir", amount: int = 1000) -> None:
    """PlaceOrder prices every line from the Buy Box read model and ignores any
    amount the caller sent, so an order cannot be placed without this row."""
    from datetime import datetime, timezone

    db.execute(
        "insert into buy_box_offer "
        "(offer_id, variant_id, seller_id, region, currency, amount, scale, "
        " is_active, offer_created_at) "
        "values (:offer, :variant, :seller, :region, 'IRR', :amount, 0, true, :created)",
        offer=offer_id, variant=uuid.uuid4(), seller=uuid.uuid4(), region=region,
        amount=amount, created=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def relay() -> OutboxRelayWorker:
    return OutboxRelayWorker(
        publisher=InProcessPublisher(EventDispatcher(build_registry(), SqlInboxStore())),
        dlq=SqlDeadLetterStore(),
        session_factory=db.session_factory(),
        batch_size=100,
        max_retries=10,
    )


def drain_relay(max_passes: int = 12) -> int:
    """Run the REAL relay until the outbox is quiet. Saga hops enqueue more
    events, so this loops rather than processing a single batch."""
    passes = 0
    for _ in range(max_passes):
        if run(relay().process_batch()) == 0:
            break
        passes += 1
    return passes


def seed_stock(offer_id: uuid.UUID, qty: int) -> None:
    with system_event_context(region="ir"):
        run(
            ReceiveStock(inv_uow, SqlSellingGateway(session_factory=db.session_factory()))
            .execute(ReceiveStockCommand(offer_id=str(offer_id), location=LOC, quantity=qty))
        )


def reserve_cmd(rid, offer_id, qty, ttl=900):
    return ReserveStockCommand(
        reservation_id=str(rid), offer_id=str(offer_id), location=LOC,
        quantity=qty, ttl_seconds=ttl,
    )


def stock_row(offer_id):
    return db.rows(
        "select on_hand, reserved, version from inventory_stock "
        "where offer_id=:o and location=:l", o=offer_id, l=LOC,
    )[0]


def outbox_types() -> list[str]:
    return [
        r["event_type"]
        for r in db.rows(
            "select event_type from outbox_messages order by created_at, occurred_at, event_id"
        )
    ]


def count_outbox(event_type: str) -> int:
    return db.scalar(
        "select count(*) from outbox_messages where event_type=:t", t=event_type
    )


# =========================================================================
# 1. reserve success
# =========================================================================
def test_reserve_success_persists_counters_reservation_and_event(offer_id):
    """Proves the whole write path lands in ONE Postgres transaction: stock
    counters, the reservation row, and the outbox event."""
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()

    with system_event_context(region="ir"):
        view = run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 8)))

    assert view.status == ReservationStatus.PENDING
    row = stock_row(offer_id)
    assert (row["on_hand"], row["reserved"]) == (10, 8)
    assert db.scalar(
        "select status from inventory_reservation where id=:r", r=rid
    ) == ReservationStatus.PENDING
    assert count_outbox("inventory.InventoryReserved") == 1
    # The envelope is populated, not defaulted — proves EventContext reached the writer.
    env = db.rows("select correlation_id, region from outbox_messages")[0]
    assert env["correlation_id"] is not None and env["region"] == "ir"


# =========================================================================
# 2. reserve over-limit failure
# =========================================================================
def test_over_limit_reserve_rolls_back_state_and_event_together(offer_id):
    """The outbox atomicity claim: a rejected reserve must leave neither a
    counter change nor an orphan event."""
    seed_stock(offer_id, 10)
    with system_event_context(region="ir"):
        run(ReserveStock(inv_uow).execute(reserve_cmd(uuid.uuid4(), offer_id, 8)))
    before = stock_row(offer_id)

    with system_event_context(region="ir"):
        with pytest.raises(ConflictError):
            run(ReserveStock(inv_uow).execute(reserve_cmd(uuid.uuid4(), offer_id, 5)))

    assert stock_row(offer_id) == before
    assert db.scalar("select count(*) from inventory_reservation") == 1
    assert count_outbox("inventory.InventoryReserved") == 1


def test_check_constraint_rejects_oversell_written_directly(offer_id):
    """Last-line backstop: even bypassing the domain entirely, Postgres refuses
    reserved > on_hand. Cannot be validated without a real database."""
    seed_stock(offer_id, 5)
    with pytest.raises(Exception) as exc:
        db.execute(
            "update inventory_stock set reserved = 6 where offer_id=:o", o=offer_id
        )
    assert "ck_inv_reserved_le_onhand" in str(exc.value)


# =========================================================================
# 3. duplicate reservation idempotency
# =========================================================================
def test_duplicate_reservation_id_applies_once(offer_id):
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()

    with system_event_context(region="ir"):
        first = run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 4)))
        second = run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 4)))

    assert first.id == second.id
    assert stock_row(offer_id)["reserved"] == 4
    assert db.scalar("select count(*) from inventory_reservation") == 1
    assert count_outbox("inventory.InventoryReserved") == 1


def test_duplicate_reservation_id_is_rejected_by_the_primary_key(offer_id):
    """The backstop under the application check."""
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 1)))

    with pytest.raises(Exception) as exc:
        db.execute(
            "insert into inventory_reservation (id, offer_id, location, quantity, status) "
            "values (:r, :o, :l, 1, 'pending')", r=rid, o=offer_id, l=LOC,
        )
    assert "inventory_reservation_pkey" in str(exc.value)


# =========================================================================
# 4. release flow
# =========================================================================
def test_release_returns_stock_and_emits_released(offer_id):
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 6)))
        view = run(ReleaseReservation(inv_uow).execute(rid))
        run(ReleaseReservation(inv_uow).execute(rid))  # idempotent

    assert view.status == ReservationStatus.RELEASED
    assert stock_row(offer_id)["reserved"] == 0
    assert count_outbox("inventory.InventoryReleased") == 1


def test_committed_reservation_cannot_be_released(offer_id):
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 6)))
        run(CommitReservation(inv_uow).execute(rid))
        with pytest.raises(ConflictError):
            run(ReleaseReservation(inv_uow).execute(rid))

    row = stock_row(offer_id)
    assert (row["on_hand"], row["reserved"]) == (4, 0)
    assert count_outbox("inventory.InventoryReleased") == 0


# =========================================================================
# 5. expiry flow
# =========================================================================
def test_expiry_returns_stock_and_emits_inventory_expired(offer_id):
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 6)))
    db.execute(
        "update inventory_reservation set expires_at = now() - interval '1 minute' "
        "where id=:r", r=rid,
    )

    with system_event_context(region="ir"):
        swept = run(ExpireReservations(inv_uow).execute())

    assert swept == 1
    assert db.scalar(
        "select status from inventory_reservation where id=:r", r=rid
    ) == ReservationStatus.EXPIRED
    assert stock_row(offer_id)["reserved"] == 0
    assert count_outbox("inventory.InventoryExpired") == 1
    assert count_outbox("inventory.InventoryReleased") == 0


def test_expiry_claim_uses_skip_locked_so_sweepers_do_not_block(offer_id, second_offer_id):
    """Two concurrent sweepers must claim DISJOINT rows. Real row locks only."""
    seed_stock(offer_id, 10)
    seed_stock(second_offer_id, 10)
    for oid in (offer_id, second_offer_id):
        rid = uuid.uuid4()
        with system_event_context(region="ir"):
            run(ReserveStock(inv_uow).execute(reserve_cmd(rid, oid, 2)))
    db.execute("update inventory_reservation set expires_at = now() - interval '1 minute'")

    async def sweep():
        with system_event_context(region="ir"):
            return await ExpireReservations(inv_uow).execute()

    a, b = run(asyncio.gather(sweep(), sweep()))

    assert a + b == 2, f"expected each reservation swept exactly once, got {a}+{b}"
    assert db.scalar(
        "select count(*) from inventory_reservation where status='expired'"
    ) == 2
    assert count_outbox("inventory.InventoryExpired") == 2


# =========================================================================
# 6. concurrent reservation conflict
# =========================================================================
def test_two_concurrent_reservations_for_the_last_unit_serialise(offer_id):
    """Real concurrent transactions on separate connections. The version CAS must
    let exactly one through; the loser re-reads and is correctly rejected."""
    seed_stock(offer_id, 1)

    async def attempt(rid):
        with system_event_context(region="ir"):
            try:
                await ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 1))
                return "won"
            except ConflictError:
                return "rejected"
            except ConcurrencyConflictError:
                return "conflict"

    results = run(asyncio.gather(attempt(uuid.uuid4()), attempt(uuid.uuid4())))

    assert results.count("won") == 1, results
    row = stock_row(offer_id)
    assert (row["on_hand"], row["reserved"]) == (1, 1)
    assert count_outbox("inventory.InventoryReserved") == 1


def test_stale_write_is_rejected_by_the_version_column(offer_id):
    seed_stock(offer_id, 5)
    version = stock_row(offer_id)["version"]
    db.execute(
        "update inventory_stock set reserved = 1, version = version + 1 where offer_id=:o",
        o=offer_id,
    )

    async def stale():
        uow = inv_uow()
        async with uow:
            item = await uow.inventory.get_item(offer_id, LOC)
            item.version = version  # pretend we read before the other writer
            item.reserve(1, uuid.uuid4())
            await uow.inventory.save_item(item)

    with pytest.raises(ConcurrencyConflictError):
        run(stale())


# =========================================================================
# 7 + 8. no oversell under concurrency
# =========================================================================
@pytest.mark.parametrize("stock,attempts", [(5, 20), (1, 12)])
def test_no_oversell_under_concurrent_load(offer_id, stock, attempts):
    """The headline invariant, under genuine parallel transactions."""
    seed_stock(offer_id, stock)

    async def attempt():
        with system_event_context(region="ir"):
            try:
                await ReserveStock(inv_uow).execute(
                    reserve_cmd(uuid.uuid4(), offer_id, 1)
                )
                return True
            except (ConflictError, ConcurrencyConflictError):
                return False

    granted = sum(run(asyncio.gather(*(attempt() for _ in range(attempts)))))

    row = stock_row(offer_id)
    assert row["reserved"] <= row["on_hand"], "OVERSOLD"
    assert granted == stock, f"granted {granted}, stock {stock}"
    assert row["reserved"] == stock
    assert count_outbox("inventory.InventoryReserved") == stock
    assert db.scalar(
        "select count(*) from inventory_reservation where status='pending'"
    ) == stock


# =========================================================================
# 9. replay safety
# =========================================================================
def test_relay_redelivery_does_not_double_apply(offer_id):
    """Re-running the relay over an already-processed event must be a no-op:
    inbox dedup plus per-use-case idempotency."""
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        run(ReserveForOrder(inv_uow).execute(
            reservation_id=rid, offer_id=offer_id, location=LOC, quantity=3))

    drain_relay()
    before_reserved = stock_row(offer_id)["reserved"]
    before_events = outbox_types()

    db.execute("update outbox_messages set published_flag = false, available_at = now()")
    drain_relay()

    assert stock_row(offer_id)["reserved"] == before_reserved
    assert outbox_types() == before_events, "replay produced new events"


def test_reserve_for_order_is_idempotent_on_reservation_id(offer_id):
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        for _ in range(3):
            run(ReserveForOrder(inv_uow).execute(
                reservation_id=rid, offer_id=offer_id, location=LOC, quantity=3))

    assert stock_row(offer_id)["reserved"] == 3
    assert count_outbox("inventory.InventoryReserved") == 1


def test_relay_delivers_saga_events_without_a_dead_letter(offer_id):
    """The C1 regression at integration level: relay-driven consumers emit their
    own events, which is exactly what failed with no EventContext."""
    seed_stock(offer_id, 10)
    add_buybox_offer(offer_id)
    with system_event_context(region="ir"):
        run(PlaceOrder(ord_uow, pricing()).execute(PlaceOrderCommand(
            customer_id=str(uuid.uuid4()), region="ir", currency="IRR",
            lines=(OrderLineInput(
                offer_id=str(offer_id), location=LOC, quantity=2),),
        )))

    drain_relay()

    assert db.scalar("select count(*) from dead_letter_messages") == 0
    assert db.scalar(
        "select count(*) from outbox_messages where published_flag = false"
    ) == 0
    assert "inventory.InventoryReserved" in outbox_types()


# =========================================================================
# 7. late payment / expiry race
# =========================================================================
def test_expiry_before_payment_fails_the_order_and_frees_stock(offer_id):
    """The ADR-0009 D1 sequence end-to-end through the real relay: an order whose
    hold lapsed must not go on to consume stock."""
    seed_stock(offer_id, 10)
    add_buybox_offer(offer_id)
    with system_event_context(region="ir"):
        order = run(PlaceOrder(ord_uow, pricing()).execute(PlaceOrderCommand(
            customer_id=str(uuid.uuid4()), region="ir", currency="IRR",
            lines=(OrderLineInput(
                offer_id=str(offer_id), location=LOC, quantity=2),),
        )))

    # Reserve the line, but stop before the payment stub advances the saga.
    run(relay().process_batch())
    db.execute(
        "update inventory_reservation set expires_at = now() - interval '1 minute' "
        "where status = 'pending'"
    )
    with system_event_context(region="ir"):
        assert run(ExpireReservations(inv_uow).execute()) >= 1

    drain_relay()

    status = db.scalar("select status from orders_saga where id=:o", o=uuid.UUID(order.id))
    assert status != "paid", "a lapsed hold must not reach PAID"
    assert stock_row(offer_id)["reserved"] == 0, "expired hold did not free stock"
    assert stock_row(offer_id)["on_hand"] == 10, "stock consumed without a valid hold"
    assert db.scalar("select count(*) from dead_letter_messages") == 0


def test_commit_of_an_expired_reservation_replies_instead_of_dead_lettering(offer_id):
    """Commit arriving after expiry must produce a fact, not a poison message."""
    seed_stock(offer_id, 10)
    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 4)))
    db.execute(
        "update inventory_reservation set expires_at = now() - interval '1 minute' "
        "where id=:r", r=rid,
    )
    with system_event_context(region="ir"):
        run(ExpireReservations(inv_uow).execute())
        run(CommitForOrder(inv_uow).execute(rid))  # must not raise

    assert count_outbox("inventory.InventoryReservationFailed") == 1
    assert count_outbox("inventory.InventoryCommitted") == 0
    assert stock_row(offer_id)["on_hand"] == 10


# =========================================================================
# 10. no hidden cross-context coupling
# =========================================================================
def test_inventory_tables_have_no_foreign_keys_to_other_contexts():
    """Schema-level proof of extractability — asserted against the real catalog,
    not the ORM definitions."""
    fks = db.rows(
        """
        select tc.table_name, ccu.table_name as target
        from information_schema.table_constraints tc
        join information_schema.constraint_column_usage ccu
          on tc.constraint_name = ccu.constraint_name
        where tc.constraint_type = 'FOREIGN KEY'
          and tc.table_name in ('inventory_stock', 'inventory_reservation')
        """
    )
    external = [
        (r["table_name"], r["target"]) for r in fks
        if not r["target"].startswith("inventory_")
    ]
    assert external == [], f"cross-context FK found: {external}"


def test_selling_acl_is_the_only_coupling_and_is_read_only(offer_id):
    """Inventory reaches Selling through one narrow existence check; an unknown
    offer is rejected and no Selling row is ever written."""
    gateway = SqlSellingGateway(session_factory=db.session_factory())
    before = db.scalar("select count(*) from offers")

    assert run(gateway.offer_exists(offer_id)) is True
    assert run(gateway.offer_exists(uuid.uuid4())) is False

    seed_stock(offer_id, 3)
    assert db.scalar("select count(*) from offers") == before


def test_inventory_writes_touch_no_other_contexts_tables(offer_id):
    """A full reserve/commit cycle must not mutate Catalog, Selling or BuyBox."""
    seed_stock(offer_id, 10)
    snapshot = {
        t: db.scalar(f"select count(*) from {t}")
        for t in ("offers", "offer_prices", "buy_box_offer", "buy_box_variant_state")
    }

    rid = uuid.uuid4()
    with system_event_context(region="ir"):
        run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, 4)))
        run(CommitReservation(inv_uow).execute(rid))

    after = {t: db.scalar(f"select count(*) from {t}") for t in snapshot}
    assert after == snapshot


def test_reconciliation_invariant_i8_holds_after_a_mixed_workload(offer_id):
    """SUM(pending) == stock.reserved — the invariant the aggregate split gave up
    structurally. Nothing in production checks it; this at least pins it here."""
    seed_stock(offer_id, 20)
    keep, release, expire = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with system_event_context(region="ir"):
        for rid, qty in ((keep, 3), (release, 4), (expire, 5)):
            run(ReserveStock(inv_uow).execute(reserve_cmd(rid, offer_id, qty)))
        run(ReleaseReservation(inv_uow).execute(release))
    db.execute(
        "update inventory_reservation set expires_at = now() - interval '1 minute' "
        "where id=:r", r=expire,
    )
    with system_event_context(region="ir"):
        run(ExpireReservations(inv_uow).execute())

    pending = db.scalar(
        "select coalesce(sum(quantity),0) from inventory_reservation "
        "where status='pending' and offer_id=:o and location=:l", o=offer_id, l=LOC,
    )
    assert pending == stock_row(offer_id)["reserved"] == 3
