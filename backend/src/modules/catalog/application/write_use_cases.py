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
from src.modules.catalog.domain.entities import Product, ProductStatus
from src.shared_kernel.application.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.platform_policy import ensure_can_platform
from src.shared_kernel.application.security import Principal

UowFactory = Callable[[], CatalogUnitOfWork]

# Which permission governs a move INTO each product status.
#
# `update` is "edit a product's attributes"; it does not govern storefront
# visibility. Publishing and archiving are separate permissions precisely so a
# role can be allowed to edit copy without being allowed to put a product in
# front of customers — and the router previously demanded `update` for all
# three, which made those permissions decorative.
#
# DRAFT maps to `publish` because moving a product out of ACTIVE is an
# unpublish: the same storefront-visibility authority, exercised in the other
# direction. Requiring more authority rather than less is the safe reading, and
# it is the one interpretive call in this table — the permission descriptions
# name publishing and archiving but not withdrawal.
STATUS_PERMISSION = {
    ProductStatus.ACTIVE: Permission.CATALOG_PRODUCTS_PUBLISH,
    ProductStatus.ARCHIVED: Permission.CATALOG_PRODUCTS_ARCHIVE,
    ProductStatus.DRAFT: Permission.CATALOG_PRODUCTS_PUBLISH,
}


class CreateProduct:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, cmd: CreateProductCommand, principal: Principal) -> ProductView:
        # AUTHORIZE FIRST — before the slug lookup, so an unauthorized caller
        # cannot use the 409 to discover which slugs are taken (HS10).
        ensure_can_platform(principal, Permission.CATALOG_PRODUCTS_CREATE)

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

    def _required(self, cmd: UpdateProductCommand) -> tuple[Permission, ...]:
        """The permissions this particular request actually needs.

        Derived from the COMMAND, never from the stored product, so the decision
        is made before anything is loaded. Editing attributes needs `update`;
        changing status needs the permission for the status being entered; doing
        both in one request needs both.

        `update` is demanded only when a non-status field is actually present.
        Otherwise a publish-only role could not publish, which would collapse the
        distinction this method exists to draw.
        """
        required: list[Permission] = []
        edits_attributes = any(
            value is not None
            for value in (
                cmd.name_fa, cmd.description, cmd.brand_id,
                cmd.category_id, cmd.is_featured,
            )
        )
        if edits_attributes:
            required.append(Permission.CATALOG_PRODUCTS_UPDATE)
        if cmd.status is not None:
            # A status the enum does not know is rejected by the domain as a
            # ValidationError; refusing here too would turn a malformed input
            # into a 403 and tell the caller nothing useful.
            permission = STATUS_PERMISSION.get(cmd.status)
            if permission is not None:
                required.append(permission)
        # An empty command edits nothing. It still requires `update` rather than
        # being waved through: a no-op that anyone may call is a probe.
        return tuple(required) or (Permission.CATALOG_PRODUCTS_UPDATE,)

    async def execute(
        self, product_id: uuid.UUID, cmd: UpdateProductCommand, principal: Principal
    ) -> ProductView:
        # AUTHORIZE FIRST — before the load, so the 404 cannot be used to probe
        # which product ids exist (HS10), and no mutation can precede the check
        # (HS9). The required permissions depend only on the command.
        ensure_can_platform(principal, *self._required(cmd))

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
