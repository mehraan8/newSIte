"""Catalog write (admin) endpoints.

Composition root for the write side: builds use cases over the Catalog UoW
factory. These routes were previously left open on the assumption that a gateway
would enforce admin RBAC; no such gateway exists in this deployment, so
authentication is enforced here with the application's own bearer-token
dependency. The coupling is limited to that single dependency.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from src.modules.identity.interface.dependencies import get_current_user

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

router = APIRouter(
    prefix="/catalog/admin",
    tags=["catalog-admin"],
    # Authentication is mandatory for every route on this router.
    dependencies=[Depends(get_current_user)],
)


def get_create_product() -> CreateProduct:
    return CreateProduct(uow_factory=SqlAlchemyCatalogUnitOfWork)


def get_update_product() -> UpdateProduct:
    return UpdateProduct(uow_factory=SqlAlchemyCatalogUnitOfWork)


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
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


@router.patch("/products/{product_id}", response_model=ProductResponse)
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
