"""Catalog write Unit of Work interface.

Exposes only Catalog's repositories. Built on the shared AbstractUnitOfWork, so
Catalog reuses the existing UoW + outbox machinery with no new patterns.
"""

from __future__ import annotations

import abc

from src.modules.catalog.application.write_ports import ProductRepository
from src.shared_kernel.application.unit_of_work import AbstractUnitOfWork


class CatalogUnitOfWork(AbstractUnitOfWork, abc.ABC):
    products: ProductRepository
