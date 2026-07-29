"""Aggregates each bounded context's public router under /api/v1.

Adding/removing a feature module = registering/unregistering one router here.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.modules.catalog.interface.admin_router import router as catalog_admin_router
from src.modules.catalog.interface.router import router as catalog_router
from src.modules.identity.interface.router import router as identity_router
from src.modules.buybox.interface.router import router as buybox_router
from src.modules.inventory.interface.router import router as inventory_router
from src.modules.ordering.interface.router import router as ordering_router
from src.modules.selling.interface.router import router as selling_router

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(identity_router)
api_v1.include_router(catalog_router)
api_v1.include_router(catalog_admin_router)
api_v1.include_router(selling_router)
api_v1.include_router(buybox_router)
api_v1.include_router(inventory_router)
api_v1.include_router(ordering_router)

# Next slices register here: cart, ordering, payment, shipping, content, admin.
