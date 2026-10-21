"""Concrete Catalog write Unit of Work — wires the product repository to the
session and registers it as the aggregate tracker. Reuses the shared
SqlAlchemyUnitOfWork (commit/outbox/rollback) unchanged.
"""

from __future__ import annotations

from src.modules.catalog.application.unit_of_work import CatalogUnitOfWork
from src.modules.catalog.infrastructure.write_repository import SqlProductRepository
from src.shared_kernel.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyCatalogUnitOfWork(SqlAlchemyUnitOfWork, CatalogUnitOfWork):
    def _build_repositories(self) -> None:
        self.products = SqlProductRepository(self.session, tracker=self)
