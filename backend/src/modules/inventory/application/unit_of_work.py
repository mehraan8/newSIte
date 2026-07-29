"""Inventory write Unit of Work interface."""

from __future__ import annotations

import abc

from src.modules.inventory.application.ports import InventoryRepository
from src.shared_kernel.application.unit_of_work import AbstractUnitOfWork


class InventoryUnitOfWork(AbstractUnitOfWork, abc.ABC):
    inventory: InventoryRepository
