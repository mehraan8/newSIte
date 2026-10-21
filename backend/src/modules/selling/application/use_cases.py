"""Selling use cases: CreateOffer, ActivateOffer, SuspendOffer, ChangeOfferPrice.

Cross-aggregate uniqueness (the configurable one-active-offer rule) is an
application-level invariant enforced via the repository + settings flag, backed
by a DB partial-unique index. Domain errors are translated at this boundary.
"""

from __future__ import annotations

import uuid
from typing import Callable

from src.modules.selling.application.dtos import (
    ChangeOfferPriceCommand,
    CreateOfferCommand,
    OfferView,
)
from src.modules.selling.application.policies import ensure_can_act_for_seller
from src.modules.selling.application.ports import CatalogGateway
from src.modules.selling.application.unit_of_work import SellingUnitOfWork
from src.modules.selling.domain.entities import Offer
from src.modules.selling.domain.exceptions import SellingDomainError
from src.shared_kernel.application.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal
from src.shared_kernel.domain.value_objects import Money

UowFactory = Callable[[], SellingUnitOfWork]


def _money(amount: int, currency: str, scale: int) -> Money:
    return Money(amount=amount, currency=currency, scale=scale)


class CreateOffer:
    def __init__(self, uow_factory: UowFactory, catalog: CatalogGateway) -> None:
        self._uow_factory = uow_factory
        self._catalog = catalog

    async def execute(self, cmd: CreateOfferCommand, principal: Principal) -> OfferView:
        # AUTHORIZE FIRST. There is no existing offer to take ownership from, so
        # the caller nominates a target seller and the nomination is honoured
        # only if it is one of THEIR memberships. This is what makes a
        # client-supplied seller_id a resource selector rather than a claim of
        # authority: naming another seller — or one's own user id — resolves to
        # no membership and is denied (ADR-0011 §7.1).
        seller_id = uuid.UUID(cmd.seller_id)
        ensure_can_act_for_seller(principal, seller_id, Permission.SELLING_OFFERS_CREATE)

        variant_id = uuid.UUID(cmd.variant_id)
        # ACL read OUTSIDE the transaction — validate the variant exists.
        if not await self._catalog.variant_exists(variant_id):
            raise NotFoundError("وَریانت موردنظر در کاتالوگ یافت نشد.")

        initial = _money(cmd.amount, cmd.currency, cmd.scale) if cmd.amount is not None else None
        compare = (
            _money(cmd.compare_at_amount, cmd.currency, cmd.scale)
            if cmd.compare_at_amount is not None
            else None
        )

        uow = self._uow_factory()
        async with uow:
            try:
                offer = Offer.create(
                    seller_id=seller_id,
                    variant_id=variant_id,
                    condition=cmd.condition,
                    currency=cmd.currency,
                    initial_price=initial,
                    compare_at=compare,
                )
            except SellingDomainError as exc:
                raise ValidationError(exc.message_fa) from exc
            await uow.offers.add(offer)
            await uow.commit()
        return OfferView.from_domain(offer)


class ActivateOffer:
    def __init__(self, uow_factory: UowFactory, *, one_active_per_variant: bool = True) -> None:
        self._uow_factory = uow_factory
        # Configurable invariant injected from settings at the composition root.
        self._one_active_per_variant = one_active_per_variant

    async def execute(self, offer_id: uuid.UUID, principal: Principal) -> OfferView:
        uow = self._uow_factory()
        async with uow:
            offer = await uow.offers.get_by_id(offer_id)
            if offer is None:
                raise NotFoundError("آفر یافت نشد.")
            # The OFFER's seller owns it — never the caller. Checked after the
            # load and before any mutation, so a denial leaves the offer intact.
            ensure_can_act_for_seller(
                principal, offer.seller_id, Permission.SELLING_OFFERS_ACTIVATE
            )

            # Configurable invariant: one active offer per seller per variant.
            if self._one_active_per_variant:
                existing = await uow.offers.find_active_for(offer.seller_id, offer.variant_id)
                if existing is not None and existing.id != offer.id:
                    raise ConflictError("برای این وَریانت قبلاً یک آفر فعال ثبت شده است.")

            try:
                offer.activate()
            except SellingDomainError as exc:
                raise ValidationError(exc.message_fa) from exc

            await uow.offers.save(offer)
            await uow.commit()
        return OfferView.from_domain(offer)


class SuspendOffer:
    """Takes an active offer off the market. Emits selling.OfferSuspended, which
    BuyBox consumes to drop the offer out of ranking."""

    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, offer_id: uuid.UUID, principal: Principal) -> OfferView:
        uow = self._uow_factory()
        async with uow:
            offer = await uow.offers.get_by_id(offer_id)
            if offer is None:
                raise NotFoundError("آفر یافت نشد.")
            ensure_can_act_for_seller(
                principal, offer.seller_id, Permission.SELLING_OFFERS_SUSPEND
            )
            try:
                offer.suspend()
            except SellingDomainError as exc:
                raise ValidationError(exc.message_fa) from exc
            await uow.offers.save(offer)
            await uow.commit()
        return OfferView.from_domain(offer)


class ChangeOfferPrice:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self, offer_id: uuid.UUID, cmd: ChangeOfferPriceCommand, principal: Principal
    ) -> OfferView:
        uow = self._uow_factory()
        async with uow:
            offer = await uow.offers.get_by_id(offer_id)
            if offer is None:
                raise NotFoundError("آفر یافت نشد.")
            ensure_can_act_for_seller(
                principal, offer.seller_id, Permission.SELLING_OFFERS_PRICE_CHANGE
            )
            compare = (
                _money(cmd.compare_at_amount, cmd.currency, cmd.scale)
                if cmd.compare_at_amount is not None
                else None
            )
            try:
                offer.change_price(_money(cmd.amount, cmd.currency, cmd.scale), compare_at=compare)
            except SellingDomainError as exc:
                raise ValidationError(exc.message_fa) from exc
            await uow.offers.save(offer)
            await uow.commit()
        return OfferView.from_domain(offer)
