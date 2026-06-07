"""Aggregates each bounded context's public router under /api/v1.

Adding/removing a feature module = registering/unregistering one router here.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.modules.catalog.interface.router import router as catalog_router
from src.modules.identity.interface.router import router as identity_router

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(identity_router)
api_v1.include_router(catalog_router)

# Next slices register here: cart, ordering, payment, shipping, content, admin.
