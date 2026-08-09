"""Selling endpoints (seller-facing).

Authentication is enforced here rather than delegated to a gateway: no gateway
exists in this deployment, and these routes create offers and set prices.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from src.modules.identity.interface.dependencies import get_current_user
from src.modules.selling.application.dtos import ChangeOfferPriceCommand
from src.shared_kernel.application.errors import CapabilityUnavailableError
from src.config.settings import settings
from src.modules.selling.application.use_cases import (
    ActivateOffer,
    ChangeOfferPrice,
    CreateOffer,
    SuspendOffer,
)
from src.modules.selling.infrastructure.catalog_acl import SqlCatalogVariantGateway
from src.modules.selling.infrastructure.unit_of_work import SqlAlchemySellingUnitOfWork
from src.modules.selling.interface.schemas import (
    ChangeOfferPriceRequest,
    CreateOfferRequest,
    OfferResponse,
)

router = APIRouter(
    prefix="/selling",
    tags=["selling"],
    # Authentication is mandatory for every route on this router.
    dependencies=[Depends(get_current_user)],
)


def get_create_offer() -> CreateOffer:
    return CreateOffer(
        uow_factory=SqlAlchemySellingUnitOfWork, catalog=SqlCatalogVariantGateway()
    )


def get_activate_offer() -> ActivateOffer:
    return ActivateOffer(
        uow_factory=SqlAlchemySellingUnitOfWork,
        one_active_per_variant=settings.selling_one_active_offer_per_variant,
    )


def get_suspend_offer() -> SuspendOffer:
    return SuspendOffer(uow_factory=SqlAlchemySellingUnitOfWork)


def get_change_price() -> ChangeOfferPrice:
    return ChangeOfferPrice(uow_factory=SqlAlchemySellingUnitOfWork)


@router.post("/offers", response_model=OfferResponse, status_code=status.HTTP_201_CREATED)
async def create_offer(body: CreateOfferRequest) -> OfferResponse:
    """INTENTIONALLY UNAVAILABLE until Phase 1G (ADR-0011 §7).

    This endpoint previously established offer ownership with
    `seller_id=str(current.id)` — the authenticated USER's id used as a MERCHANT
    identity. That was the correct emergency fix for the body-supplied
    `seller_id` impersonation hole, but it collapses `User == Seller`, which the
    target model forbids: a Seller is a business owned by Merchant, and a User
    reaches it only through a SellerMember grant.

    There is no legitimate way to resolve `Principal -> SellerMember -> Seller.id`
    until Merchant (Phase 1C) and membership resolution (Phase 1F) exist. Rather
    than keep minting rows whose `seller_id` means the wrong thing — and which
    would then propagate into the BuyBox projection and into immutable outbox
    events — the endpoint fails explicitly.

    Deliberately NOT done here: no fabricated seller, no temporary seller table,
    no body-supplied `seller_id` (the schema still forbids it), no JWT seller
    claim, no ambient state. Authentication is still enforced by the router-level
    dependency, so an anonymous caller gets 401 and an authenticated one gets 503
    — the failure is about a missing capability, not a missing credential.

    Phase 1G restores this by taking the target seller from the caller's
    memberships and verifying it with `Principal.can_for_seller`.
    """
    raise CapabilityUnavailableError(
        "ثبت آفر تا زمان راه‌اندازی هویت فروشنده در دسترس نیست."
    )


@router.post("/offers/{offer_id}/activate", response_model=OfferResponse)
async def activate_offer(
    offer_id: uuid.UUID, uc: ActivateOffer = Depends(get_activate_offer)
) -> OfferResponse:
    return OfferResponse.from_view(await uc.execute(offer_id))


@router.post("/offers/{offer_id}/suspend", response_model=OfferResponse)
async def suspend_offer(
    offer_id: uuid.UUID, uc: SuspendOffer = Depends(get_suspend_offer)
) -> OfferResponse:
    return OfferResponse.from_view(await uc.execute(offer_id))


@router.patch("/offers/{offer_id}/price", response_model=OfferResponse)
async def change_offer_price(
    offer_id: uuid.UUID,
    body: ChangeOfferPriceRequest,
    uc: ChangeOfferPrice = Depends(get_change_price),
) -> OfferResponse:
    cmd = ChangeOfferPriceCommand(
        amount=body.amount,
        currency=body.currency,
        scale=body.scale,
        compare_at_amount=body.compare_at_amount,
    )
    return OfferResponse.from_view(await uc.execute(offer_id, cmd))
