"""Selling write Unit of Work interface — exposes only Selling's repositories."""

from __future__ import annotations

import abc

from src.modules.selling.application.ports import OfferRepository
from src.shared_kernel.application.unit_of_work import AbstractUnitOfWork


class SellingUnitOfWork(AbstractUnitOfWork, abc.ABC):
    offers: OfferRepository
