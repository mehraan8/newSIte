"""Concrete async-SQLAlchemy Unit of Work (ADR-0007).

Wraps one AsyncSession + transaction. Subclasses build their context's
repositories in `_build_repositories`. The commit sequence is the heart of the
pattern: flush state -> drain events -> write outbox -> single atomic commit.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared_kernel.application.unit_of_work import AbstractUnitOfWork
from src.shared_kernel.infrastructure.database import SessionFactory
from src.shared_kernel.infrastructure.outbox.writer import write_events_to_outbox


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession] = SessionFactory
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork accessed outside an 'async with' block")
        return self._session

    async def __aenter__(self) -> "SqlAlchemyUnitOfWork":
        await super().__aenter__()
        self._session = self._session_factory()
        self._build_repositories()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await super().__aexit__(exc_type, exc, tb)  # rollback if not committed
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None

    def _build_repositories(self) -> None:
        """Subclasses bind their repositories to `self.session` and `self`
        (as the aggregate tracker). Base context has none."""

    async def commit(self) -> None:
        # 1) Persist state first so DB-generated values/versions are final.
        await self.session.flush()
        # 2) Drain domain events from every tracked aggregate.
        events = self.collect_new_events()
        # 3) Stage outbox rows on the SAME session (same transaction).
        write_events_to_outbox(self.session, events)
        # 4) One atomic commit: state + outbox succeed or fail together.
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self._session is not None:
            await self._session.rollback()
        self._discard_tracked()
