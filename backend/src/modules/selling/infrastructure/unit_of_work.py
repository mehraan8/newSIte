"""Concrete Selling UoW — reuses the shared SqlAlchemyUnitOfWork unchanged."""

from __future__ import annotations

from src.modules.selling.application.unit_of_work import SellingUnitOfWork
from src.modules.selling.infrastructure.repository import SqlOfferRepository
from src.shared_kernel.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemySellingUnitOfWork(SqlAlchemyUnitOfWork, SellingUnitOfWork):
    def _build_repositories(self) -> None:
        self.offers = SqlOfferRepository(self.session, tracker=self)
