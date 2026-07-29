"""Concrete Ordering UoW — reuses the shared SqlAlchemyUnitOfWork unchanged."""

from __future__ import annotations

from src.modules.ordering.application.unit_of_work import OrderingUnitOfWork
from src.modules.ordering.infrastructure.repository import (
    SqlOrderIdempotencyStore,
    SqlOrderRepository,
)
from src.shared_kernel.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyOrderingUnitOfWork(SqlAlchemyUnitOfWork, OrderingUnitOfWork):
    def _build_repositories(self) -> None:
        self.orders = SqlOrderRepository(self.session, tracker=self)
        self.idempotency = SqlOrderIdempotencyStore(self.session)
