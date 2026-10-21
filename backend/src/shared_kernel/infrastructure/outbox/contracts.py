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


class TransactionMode:
    """How a consumer's DB work relates to the relay's transaction.

    Both modes are legitimate and both are in use; what is NOT acceptable is
    leaving it implicit, because the two give different delivery guarantees and a
    reader cannot tell them apart from the handler signature.

    RELAY_SESSION — the handler writes through the `session` it is handed. Its
      writes, its inbox row and the outbox 'published' mark commit together, so
      DB effects are exactly-once. The handler must NOT commit, and must NOT
      publish events (it has no UoW to drain them into the outbox).

    OWN_UNIT_OF_WORK — the handler ignores `session` and opens its own UoW. This
      is required for any consumer that must EMIT events, because only a UoW
      drains aggregates into the outbox in the same transaction as the state
      change. The cost: that commit lands BEFORE the dispatcher records the inbox
      row, so a crash in between replays the handler. Delivery is therefore
      at-least-once and the handler MUST be internally idempotent — its own
      business key, not the inbox, is what makes it safe.
    """

    RELAY_SESSION = "relay_session"
    OWN_UNIT_OF_WORK = "own_unit_of_work"
    ALL = frozenset({RELAY_SESSION, OWN_UNIT_OF_WORK})


@runtime_checkable
class EventHandler(Protocol):
    """A consumer. `name` is the stable inbox identity; `event_type` is what it
    subscribes to; `transaction_mode` declares which transaction its writes join
    (see TransactionMode — the registry rejects handlers that do not declare it).

    External side effects (SMS, HTTP) are at-least-once in BOTH modes — defer
    them via a new outbox event instead of calling them inline."""

    name: str
    event_type: str
    transaction_mode: str

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
