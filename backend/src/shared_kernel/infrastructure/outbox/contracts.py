"""Relay contracts (DB-free, unit-testable).

Defines the seams between the four collaborators so each can be swapped:
  OutboxRelayWorker  -> drains the table, owns retries/DLQ          (relay.py)
  EventPublisher     -> delivery seam (in-process now, Kafka later) (dispatcher.py)
  EventDispatcher    -> routes one event to its handlers w/ inbox   (dispatcher.py)
  EventHandler       -> consumer business logic                     (per context)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class IncomingEvent:
    """Read-only view of an outbox row handed to consumers."""

    event_id: uuid.UUID
    idempotency_key: str
    event_type: str
    event_version: int
    aggregate_id: str
    aggregate_type: str
    occurred_at: datetime
    payload: dict
    correlation_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    region: str = "global"
    pii_refs: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class EventHandler(Protocol):
    """A consumer. `name` is the stable inbox identity; `event_type` is what it
    subscribes to. `handle` receives the SAME session as the relay tx, so its DB
    writes commit atomically with the inbox record (exactly-once for DB effects).
    External side effects (SMS, HTTP) are at-least-once — defer them via a new
    outbox event instead of calling them inline."""

    name: str
    event_type: str

    async def handle(self, session: Any, event: IncomingEvent) -> None: ...


class InboxStore(Protocol):
    """Consumer-side dedup: (handler_name, idempotency_key) processed-once."""

    async def already_processed(
        self, session: Any, handler_name: str, idempotency_key: str
    ) -> bool: ...

    async def mark_processed(
        self, session: Any, handler_name: str, event: IncomingEvent
    ) -> None: ...


class EventPublisher(Protocol):
    """Delivery seam. Phase 1 = in-process dispatch (shares the tx). Future =
    Kafka producer (ignores session; consumers then own their own inbox)."""

    async def publish(self, session: Any, event: IncomingEvent) -> None: ...


class DeadLetterStore(Protocol):
    async def dead_letter(self, session: Any, row: Any, error: str) -> None: ...
