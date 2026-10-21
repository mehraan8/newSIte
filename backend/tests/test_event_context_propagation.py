"""EventContext must reach every event producer, not just HTTP handlers.

The outbox writer REQUIRES an EventContext, and only the request middleware
injected one. Relay-driven consumers and background jobs commit through their own
UoW, so every saga hop that emitted an event raised EventContextError, retried 10
times and dead-lettered. Nothing caught it because every test fake commits
without going through write_events_to_outbox.

Run: pytest tests/test_event_context_propagation.py
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timezone, datetime

import pytest

from src.shared_kernel.application.event_context import (
    EventContextError,
    current_event_context,
    require_event_context,
)
from src.shared_kernel.infrastructure.outbox.contracts import (
    IncomingEvent,
    TransactionMode,
)
from src.shared_kernel.infrastructure.outbox.dispatcher import EventDispatcher
from src.shared_kernel.infrastructure.outbox.registry import HandlerRegistry

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CORRELATION = uuid.UUID("c0000000-0000-0000-0000-000000000001")
EVENT_ID = uuid.UUID("e0000000-0000-0000-0000-000000000001")
TENANT = uuid.UUID("70000000-0000-0000-0000-000000000001")


def run(coro):
    return asyncio.run(coro)


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def begin_nested(self):
        return _Nested()


class FakeInbox:
    def __init__(self):
        self.marked = []

    async def already_processed(self, session, handler_name, idempotency_key):
        return False

    async def mark_processed(self, session, handler_name, event):
        self.marked.append(handler_name)


class RecordingHandler:
    """Stands in for any saga participant that emits an event."""

    name = "test.records_context"
    event_type = "test.SomethingHappened"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self):
        self.seen = None
        self.error = None

    async def handle(self, session, event):
        self.seen = current_event_context()
        try:
            require_event_context()          # exactly what the outbox writer does
        except EventContextError as exc:
            self.error = exc


def an_event(**overrides) -> IncomingEvent:
    fields = dict(
        event_id=EVENT_ID, idempotency_key=str(EVENT_ID),
        event_type="test.SomethingHappened", event_version=1,
        aggregate_id=str(uuid.uuid4()), aggregate_type="Thing",
        occurred_at=T0, payload={}, correlation_id=CORRELATION,
        causation_id=None, tenant_id=TENANT, region="ir",
    )
    fields.update(overrides)
    return IncomingEvent(**fields)


def dispatch(event) -> RecordingHandler:
    handler = RecordingHandler()
    registry = HandlerRegistry()
    registry.register(handler)
    dispatcher = EventDispatcher(registry, FakeInbox())
    run(dispatcher.dispatch(FakeSession(), event))
    return handler


def test_handler_can_publish_events():
    """The regression: without an injected context this raised EventContextError
    and the whole checkout saga dead-lettered."""
    handler = dispatch(an_event())
    assert handler.error is None


def test_correlation_id_is_carried_through_the_chain():
    assert dispatch(an_event()).seen.correlation_id == CORRELATION


def test_the_handled_event_becomes_the_causation_id():
    assert dispatch(an_event()).seen.causation_id == EVENT_ID


def test_tenant_and_region_ride_along():
    seen = dispatch(an_event()).seen
    assert (seen.tenant_id, seen.region) == (TENANT, "ir")


def test_an_event_without_correlation_still_yields_a_publishable_context():
    """Defensive: a row predating the envelope must not poison the relay."""
    handler = dispatch(an_event(correlation_id=None))
    assert handler.error is None
    assert handler.seen.correlation_id == EVENT_ID


def test_context_does_not_leak_after_dispatch():
    dispatch(an_event())
    assert current_event_context().correlation_id is None


def test_context_is_reset_even_when_a_handler_raises():
    class Boom(RecordingHandler):
        async def handle(self, session, event):
            raise RuntimeError("handler failed")

    registry = HandlerRegistry()
    registry.register(Boom())
    dispatcher = EventDispatcher(registry, FakeInbox())

    with pytest.raises(RuntimeError):
        run(dispatcher.dispatch(FakeSession(), an_event()))
    assert current_event_context().correlation_id is None


# === H1: consumer transaction model is declared, not implied =============
def test_registry_rejects_a_handler_that_does_not_declare_its_mode():
    """The whole point: a future consumer cannot silently inherit a guarantee it
    does not have."""
    class Undeclared:
        name = "test.undeclared"
        event_type = "test.X"

        async def handle(self, session, event):
            pass

    with pytest.raises(ValueError, match="transaction_mode"):
        HandlerRegistry().register(Undeclared())


def test_registry_rejects_an_invalid_mode():
    class Bogus:
        name = "test.bogus"
        event_type = "test.X"
        transaction_mode = "whatever"

        async def handle(self, session, event):
            pass

    with pytest.raises(ValueError, match="transaction_mode"):
        HandlerRegistry().register(Bogus())


def test_every_registered_handler_declares_a_valid_mode():
    pytest.importorskip("sqlalchemy", reason="relay wiring pulls in the DB layer")
    from src.shared_kernel.infrastructure.outbox.runner import build_registry

    registry = build_registry()
    for event_type in registry.event_types():
        for handler in registry.handlers_for(event_type):
            assert handler.transaction_mode in TransactionMode.ALL, handler.name


def test_event_emitting_consumers_are_declared_own_unit_of_work():
    """Emitting requires a UoW to drain the outbox, so these cannot be
    relay-session handlers. Pinned so the two models stay distinguishable."""
    pytest.importorskip("sqlalchemy", reason="relay wiring pulls in the DB layer")
    from src.shared_kernel.infrastructure.outbox.runner import build_registry

    registry = build_registry()
    by_name = {
        h.name: h
        for et in registry.event_types()
        for h in registry.handlers_for(et)
    }
    emitters = [n for n in by_name if n.startswith(("inventory.", "ordering."))]
    assert emitters, "expected saga participants to be registered"
    for name in emitters:
        assert by_name[name].transaction_mode == TransactionMode.OWN_UNIT_OF_WORK, name

    for name in ("notification.on_user_registered", "buybox.on_offer_created"):
        assert by_name[name].transaction_mode == TransactionMode.RELAY_SESSION, name


# === background sweeper ===================================================
def test_expiry_worker_sweeps_inside_an_event_context():
    """The sweeper is a non-HTTP producer, so it must inject its own context or
    every pass that actually expires something fails at commit."""
    from src.modules.inventory.infrastructure import expiry_worker

    seen = {}

    class FakeExpire:
        def __init__(self, uow_factory):
            pass

        async def execute(self, *, limit):
            seen["ctx"] = current_event_context()
            seen["limit"] = limit
            return 3

    original = expiry_worker.ExpireReservations
    expiry_worker.ExpireReservations = FakeExpire
    try:
        expired = run(expiry_worker.sweep_once(limit=7, region="ir"))
    finally:
        expiry_worker.ExpireReservations = original

    assert expired == 3
    assert seen["limit"] == 7
    assert seen["ctx"].correlation_id is not None      # publishable
    assert seen["ctx"].region == "ir"
    assert current_event_context().correlation_id is None   # and cleaned up
