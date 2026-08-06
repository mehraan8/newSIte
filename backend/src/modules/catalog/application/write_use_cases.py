"""Catalog write use cases. Orchestrate the aggregate + UoW; emit no events
directly (the aggregate records them, the UoW drains them to the outbox).

Each use case opens a fresh UoW per execution via the injected factory.
"""

from __future__ import annotations

import uuid
from typing import Callable

from src.modules.catalog.application.unit_of_work import CatalogUnitOfWork
from src.modules.catalog.application.write_dtos import (
    CreateProductCommand,
    ProductView,
    UpdateProductCommand,
)
from src.modules.catalog.domain.entities import Product
from src.shared_kernel.application.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

UowFactory = Callable[[], CatalogUnitOfWork]


class CreateProduct:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, cmd: CreateProductCommand) -> ProductView:
        uow = self._uow_factory()
        async with uow:
            if await uow.products.get_by_slug(cmd.slug) is not None:
                raise ConflictError("محصولی با این نامک از قبل وجود دارد.")

            # The Product aggregate signals malformed input with plain ValueError
            # (invalid slug, blank name_fa, blank/duplicate sku). Translate it to a
            # transport-mappable application error HERE, at the boundary, exactly as
            # Selling does for SellingDomainError — otherwise it reaches the
            # catch-all handler and a bad request is reported as a 500.
            try:
                product = Product.create(
                    product_type_id=cmd.product_type_id,
                    category_id=cmd.category_id,
                    slug=cmd.slug,
                    name_fa=cmd.name_fa,
                    brand_id=cmd.brand_id,
                    description=cmd.description,
                    is_featured=cmd.is_featured,
                )
                for v in cmd.variants:
                    product.add_variant(
                        v.sku, variant_axis_values=v.variant_axis_values, barcode=v.barcode
                    )
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            await uow.products.add(product)
            await uow.commit()      # state + ProductCreated outbox row, atomic
        return ProductView.from_domain(product)


class UpdateProduct:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, product_id: uuid.UUID, cmd: UpdateProductCommand) -> ProductView:
        uow = self._uow_factory()
        async with uow:
            product = await uow.products.get_by_id(product_id)
            if product is None:
                raise NotFoundError("محصول یافت نشد.")

            # Same boundary translation as CreateProduct: blank name_fa and an
            # invalid status value are caller errors, not server faults.
            try:
                product.update_details(
                    name_fa=cmd.name_fa,
                    description=cmd.description,
                    brand_id=cmd.brand_id,
                    category_id=cmd.category_id,
                    is_featured=cmd.is_featured,
                )
                if cmd.status is not None:
                    product.change_status(cmd.status)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            await uow.products.save(product)
            await uow.commit()      # state + ProductUpdated outbox row, atomic
        return ProductView.from_domain(product)
