"""Merchant context Unit of Work interface.

Exposes only Merchant's repositories, so a Merchant use case cannot reach into
another context's data. Cross-context effects go through events/outbox, never a
shared UoW.
"""

from __future__ import annotations

import abc

from src.modules.merchant.application.ports import (
    SellerMemberRepository,
    SellerRepository,
)
from src.shared_kernel.application.unit_of_work import AbstractUnitOfWork


class MerchantUnitOfWork(AbstractUnitOfWork, abc.ABC):
    # Declared for typing; bound by the concrete implementation at enter time.
    sellers: SellerRepository
    members: SellerMemberRepository
