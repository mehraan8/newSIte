"""Concrete Inventory UoW — reuses the shared SqlAlchemyUnitOfWork unchanged."""

from __future__ import annotations

from src.modules.inventory.application.unit_of_work import InventoryUnitOfWork
from src.modules.inventory.infrastructure.repository import SqlInventoryRepository
from src.shared_kernel.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyInventoryUnitOfWork(SqlAlchemyUnitOfWork, InventoryUnitOfWork):
    def _build_repositories(self) -> None:
        self.inventory = SqlInventoryRepository(self.session, tracker=self)
