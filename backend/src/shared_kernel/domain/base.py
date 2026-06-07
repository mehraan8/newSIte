"""Domain building blocks shared by every bounded context.

Pure Python only — no SQLAlchemy, FastAPI, Pydantic, or Redis here. The
dependency rule points inward: the domain knows nothing about infrastructure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


@dataclass(frozen=True)
class DomainEvent:
    """Something that happened in the domain, worth reacting to."""

    event_id: uuid.UUID = field(default_factory=new_uuid)
    occurred_at: datetime = field(default_factory=utcnow)

    @property
    def event_type(self) -> str:
        return type(self).__name__


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
        self._events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        events, self._events = self._events, []
        return events
