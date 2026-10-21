"""Event dispatcher + in-process publisher (DB-free logic).

The dispatcher routes one event to every subscribed handler with PER-HANDLER
idempotency and PER-HANDLER savepoints:

  * inbox check first  -> already-done handlers are skipped
  * begin_nested()     -> each handler + its inbox record is its own savepoint
  * a failing handler rolls back ONLY its own savepoint and re-raises, so
    sibling handlers that already succeeded keep their progress when the relay
    commits the outer transaction (partial-failure progress).
  * an EventContext is injected for the duration, because handlers that emit
    events cannot commit without one.

What the inbox actually guarantees depends on the handler's TransactionMode:

  RELAY_SESSION (notification, buybox) — the handler writes through the relay
    session, so its writes + inbox row + 'published' mark commit together.
    Exactly-once for DB effects.

  OWN_UNIT_OF_WORK (inventory, ordering) — the handler commits its own UoW
    BEFORE the dispatcher records the inbox row. A crash in that window replays
    the handler, so delivery is at-least-once and the inbox is an optimisation,
    not the safety property. These handlers are safe because their use cases are
    idempotent on a business key (reservation_id, order/line status).

Do not assume the first guarantee for a handler running in the second mode.
"""

from __future__ import annotations

from typing import Any

from src.shared_kernel.application.event_context import (
    EventContext,
    reset_event_context,
    set_event_context,
)
from src.shared_kernel.infrastructure.outbox.contracts import (
    EventPublisher,
    IncomingEvent,
    InboxStore,
)
from src.shared_kernel.infrastructure.outbox.registry import HandlerRegistry


class EventDispatcher:
    def __init__(self, registry: HandlerRegistry, inbox: InboxStore) -> None:
        self._registry = registry
        self._inbox = inbox

    async def dispatch(self, session: Any, event: IncomingEvent) -> None:
        # Handlers that react by producing events (every saga participant) commit
        # through a UoW, and the outbox writer REQUIRES an EventContext. Only the
        # HTTP middleware injected one, so relay-driven handlers raised
        # EventContextError and dead-lettered. Chain the envelope per ADR-0008:
        # the flow's correlation_id is carried forward, and the event being
        # handled becomes the causation_id of whatever the handler emits.
        token = set_event_context(
            EventContext(
                correlation_id=event.correlation_id or event.event_id,
                causation_id=event.event_id,
                tenant_id=event.tenant_id,
                region=event.region,
            )
        )
        try:
            for handler in self._registry.handlers_for(event.event_type):
                if await self._inbox.already_processed(
                    session, handler.name, event.idempotency_key
                ):
                    continue
                # Per-handler savepoint: rolls back this handler's DB writes + its
                # inbox row together if it raises; succeeded siblings are unaffected.
                async with session.begin_nested():
                    await handler.handle(session, event)
                    await self._inbox.mark_processed(session, handler.name, event)
                # Exception propagates to the relay, which schedules a retry of the
                # row; already-marked handlers are skipped on the next attempt.
        finally:
            reset_event_context(token)


class InProcessPublisher(EventPublisher):
    """Phase-1 delivery: run handlers inside the relay's transaction.

    RELAY_SESSION handlers share that session, so their DB writes + inbox records
    + the outbox 'published' mark commit atomically (exactly-once DB effects).
    OWN_UNIT_OF_WORK handlers commit separately and are at-least-once — see the
    module docstring. Swap this for another publisher without touching the relay.
    """

    def __init__(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher

    async def publish(self, session: Any, event: IncomingEvent) -> None:
        await self._dispatcher.dispatch(session, event)
