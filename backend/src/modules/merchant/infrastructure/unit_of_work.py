"""Concrete Merchant Unit of Work — wires Merchant repositories to the session.

Both Seller and SellerMember are event-bearing aggregates, so both repositories
receive `self` as the Tracker: their events drain to the outbox in the same
transaction as the state change, exactly as every other context does.
"""

from __future__ import annotations

from src.modules.merchant.application.unit_of_work import MerchantUnitOfWork
from src.modules.merchant.infrastructure.repository import (
    SqlSellerMemberRepository,
    SqlSellerRepository,
)
from src.shared_kernel.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyMerchantUnitOfWork(SqlAlchemyUnitOfWork, MerchantUnitOfWork):
    def _build_repositories(self) -> None:
        self.sellers = SqlSellerRepository(self.session, tracker=self)
        self.members = SqlSellerMemberRepository(self.session, tracker=self)
