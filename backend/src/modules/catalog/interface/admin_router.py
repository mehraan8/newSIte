"""Catalog write (admin) endpoints.

Composition root for the write side: builds use cases over the Catalog UoW
factory. These routes were once left open on the assumption that a gateway would
enforce admin RBAC; no such gateway exists in this deployment.

AUTHENTICATION alone was not enough, and for a while that is all these routes
had: the security audit confirmed that any logged-in customer could create a
product and edit anyone else's. Each route now also demands the PLATFORM
permission for the operation it performs, checked from the assembled Principal
before the use case runs.

The permissions are deliberately per-operation rather than one blanket
"catalog admin" grant: `catalog_admin` holds both, but a future read-only or
publish-only role needs the distinction to exist.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from src.api.dependencies import get_principal
from src.api.guards import require_permission
from src.shared_kernel.application.permissions import Permission

from src.modules.catalog.application.write_dtos import (
    CreateProductCommand,
    UpdateProductCommand,
    VariantInput,
)
from src.modules.catalog.application.write_use_cases import CreateProduct, UpdateProduct
from src.modules.catalog.infrastructure.unit_of_work import SqlAlchemyCatalogUnitOfWork
from src.modules.catalog.interface.admin_schemas import (
    CreateProductRequest,
    ProductResponse,
    UpdateProductRequest,
)

# Authentication is mandatory for every route on this router — a FLOOR, so a
# route added without its own guard still cannot be reached anonymously. The
# per-route guards below carry the actual authorization.
#
# Deliberately NOT a router-wide `catalog.products.read` guard: that permission
# belongs to super_admin alone in the approved baseline, so requiring it here
# would lock `catalog_admin` — the role whose entire purpose is this router —
# out of its own endpoints. A guard that keeps the right people out is as much a
# defect as one that lets the wrong people in.
router = APIRouter(
    prefix="/catalog/admin",
    tags=["catalog-admin"],
    dependencies=[Depends(get_principal)],
)


def get_create_product() -> CreateProduct:
    return CreateProduct(uow_factory=SqlAlchemyCatalogUnitOfWork)


def get_update_product() -> UpdateProduct:
    return UpdateProduct(uow_factory=SqlAlchemyCatalogUnitOfWork)


@router.post(
    "/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.CATALOG_PRODUCTS_CREATE))],
)
async def create_product(
    body: CreateProductRequest,
    uc: CreateProduct = Depends(get_create_product),
) -> ProductResponse:
    cmd = CreateProductCommand(
        product_type_id=body.product_type_id,
        category_id=body.category_id,
        slug=body.slug,
        name_fa=body.name_fa,
        brand_id=body.brand_id,
        description=body.description,
        is_featured=body.is_featured,
        variants=tuple(
            VariantInput(sku=v.sku, variant_axis_values=v.variant_axis_values, barcode=v.barcode)
            for v in body.variants
        ),
    )
    return ProductResponse.from_view(await uc.execute(cmd))


@router.patch(
    "/products/{product_id}",
    response_model=ProductResponse,
    dependencies=[Depends(require_permission(Permission.CATALOG_PRODUCTS_UPDATE))],
)
async def update_product(
    product_id: uuid.UUID,
    body: UpdateProductRequest,
    uc: UpdateProduct = Depends(get_update_product),
) -> ProductResponse:
    cmd = UpdateProductCommand(
        name_fa=body.name_fa,
        description=body.description,
        brand_id=body.brand_id,
        category_id=body.category_id,
        is_featured=body.is_featured,
        status=body.status,
    )
    return ProductResponse.from_view(await uc.execute(product_id, cmd))
