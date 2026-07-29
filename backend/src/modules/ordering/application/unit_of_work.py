"""Ordering write Unit of Work interface."""

from __future__ import annotations

import abc

from src.modules.ordering.application.ports import (
    OrderIdempotencyStore,
    OrderRepository,
)
from src.shared_kernel.application.unit_of_work import AbstractUnitOfWork


class OrderingUnitOfWork(AbstractUnitOfWork, abc.ABC):
    orders: OrderRepository
    idempotency: OrderIdempotencyStore
