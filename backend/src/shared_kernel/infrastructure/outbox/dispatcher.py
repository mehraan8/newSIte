"""Event dispatcher + in-process publisher (DB-free logic).

The dispatcher routes one event to every subscribed handler with PER-HANDLER
idempotency and PER-HANDLER savepoints:

  * inbox check first  -> already-done handlers are skipped (idempotent)
  * begin_nested()     -> each handler + its inbox record is its own savepoint
  * a failing handler rolls back ONLY its own savepoint and re-raises, so
    sibling handlers that already succeeded keep their progress when the relay
    commits the outer transaction (partial-failure progress).
"""

from __future__ import annotations

from typing import Any

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
        for handler in self._registry.handlers_for(event.event_type):
            if await self._inbox.already_processed(session, handler.name, event.idempotency_key):
                continue
            # Per-handler savepoint: rolls back this handler's DB writes + its
            # inbox row together if it raises; succeeded siblings are unaffected.
            async with session.begin_nested():
                await handler.handle(session, event)
                await self._inbox.mark_processed(session, handler.name, event)
            # Exception propagates to the relay, which schedules a retry of the
            # row; already-marked handlers are skipped on the next attempt.


class InProcessPublisher(EventPublisher):
    """Phase-1 delivery: run handlers in the relay's own transaction.

    Because handlers share the relay session, their DB writes + inbox records +
    the outbox 'published' mark all commit atomically -> exactly-once DB effects.
    Swap this for a KafkaPublisher later without touching the relay.
    """

    def __init__(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher

    async def publish(self, session: Any, event: IncomingEvent) -> None:
        await self._dispatcher.dispatch(session, event)
