"""Application-owned transactional boundary (ADR-0007).

The Unit of Work — NOT FastAPI, NOT the web layer — owns the transaction. It
tracks the aggregates touched in one business operation, and on commit it drains
their domain events and hands them to the outbox writer *inside the same DB
transaction* (atomic state + events).

This module is pure application/domain: it imports nothing from SQLAlchemy,
FastAPI, or Redis. The concrete engine lives in infrastructure.
"""

from __future__ import annotations

import abc
from typing import Protocol, runtime_checkable

from src.shared_kernel.domain.base import AggregateRoot, DomainEvent


@runtime_checkable
class Tracker(Protocol):
    """What a repository needs from the UoW: a way to register aggregates so
    their events can be collected at commit time. Keeps repositories decoupled
    from the concrete UoW class."""

    def track(self, aggregate: AggregateRoot) -> None: ...


class AbstractUnitOfWork(abc.ABC):
    """Async context manager owning one transaction for one business operation.

    Usage (application layer):

        async with uow:
            user = await uow.users.get_by_mobile(mobile)
            user.mark_logged_in()
            await uow.commit()

    Anything not explicitly committed is rolled back on exit.
    """

    def __init__(self) -> None:
        self._seen: list[AggregateRoot] = []
        self._committed: bool = False

    async def __aenter__(self) -> "AbstractUnitOfWork":
        self._seen = []
        self._committed = False
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Safety net: an un-committed (or failed) transaction never leaks.
        if not self._committed:
            await self.rollback()

    # -- aggregate tracking (called by repositories) -----------------------
    def track(self, aggregate: AggregateRoot) -> None:
        if aggregate not in self._seen:
            self._seen.append(aggregate)

    def collect_new_events(self) -> list[tuple[AggregateRoot, DomainEvent]]:
        """Drain events from every tracked aggregate. Called once, at commit."""
        collected: list[tuple[AggregateRoot, DomainEvent]] = []
        for aggregate in self._seen:
            for event in aggregate.pull_events():
                collected.append((aggregate, event))
        return collected

    def _discard_tracked(self) -> None:
        """On rollback, drain-and-drop so a reused aggregate can't leak events."""
        for aggregate in self._seen:
            aggregate.pull_events()
        self._seen.clear()

    # -- transaction control (concrete in infrastructure) ------------------
    @abc.abstractmethod
    async def commit(self) -> None: ...

    @abc.abstractmethod
    async def rollback(self) -> None: ...
