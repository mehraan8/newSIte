"""Domain building blocks shared by every bounded context.

Pure Python only — no SQLAlchemy, FastAPI, Pydantic, or Redis here. The
dependency rule points inward: the domain knows nothing about infrastructure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import ClassVar


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


@dataclass(frozen=True)
class DomainEvent:
    """A domain fact, worth reacting to.

    Carries only what the *aggregate* legitimately knows. Ambient envelope
    metadata that belongs to the request/runtime — correlation_id, causation_id,
    tenant_id, region — is NOT recorded here; it is stamped at the UoW boundary
    from EventContext. This keeps the domain free of transport concerns.

    `pii_refs` holds opaque vault tokens only — raw PII must never appear in an
    event (it lands in the serialized payload otherwise). See PII strategy.
    """

    event_id: uuid.UUID = field(default_factory=new_uuid)
    occurred_at: datetime = field(default_factory=utcnow)
    aggregate_id: uuid.UUID | None = None      # stamped by record_event
    aggregate_type: str = ""                    # stamped by record_event
    idempotency_key: str | None = None          # defaults to event_id at enrich time
    pii_refs: tuple[str, ...] = ()              # vault tokens, never raw PII

    # Per-event-type schema version. Bump in a subclass on a breaking change.
    event_version: ClassVar[int] = 1

    @property
    def event_type(self) -> str:
        """Fully-qualified, stable name: `<context>.<EventName>`."""
        parts = type(self).__module__.split(".")
        context = "shared"
        if "modules" in parts:
            i = parts.index("modules")
            if i + 1 < len(parts):
                context = parts[i + 1]
        return f"{context}.{type(self).__name__}"


class Entity:
    """Has identity; equality is by id, not by attribute values."""

    id: object

    def __eq__(self, other: object) -> bool:
        return isinstance(other, type(self)) and getattr(other, "id", None) == self.id

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.id))


class AggregateRoot(Entity):
    """Consistency boundary; collects domain events to be published."""

    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def record_event(self, event: DomainEvent) -> None:
        # Stamp aggregate linkage if the producer didn't set it, so events stay
        # terse to construct while the outbox always has aggregate_id/type.
        if event.aggregate_id is None:
            event = replace(
                event, aggregate_id=self.id, aggregate_type=type(self).__name__
            )
        self._events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        events, self._events = self._events, []
        return events
