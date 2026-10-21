"""Selling endpoints (seller-facing).

Authentication is enforced here rather than delegated to a gateway: no gateway
exists in this deployment, and these routes create offers and set prices.

Authorization is NOT decided here. Every route hands the `Principal` to its use
case, which resolves the offer's owning seller and checks the membership before
mutating (ADR-0011 §9). Deciding it at the router would mean deciding it before
the offer is loaded — and the offer is the only thing that knows who owns it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from src.api.dependencies import get_principal
from src.modules.selling.application.dtos import ChangeOfferPriceCommand, CreateOfferCommand
from src.shared_kernel.application.security import Principal
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
    # Authentication is mandatory for every route on this router — declared at the
    # router so a future route cannot be added without it. FastAPI caches the
    # dependency per request, so the routes below reuse this same Principal
    # rather than resolving memberships a second time.
    dependencies=[Depends(get_principal)],
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
async def create_offer(
    body: CreateOfferRequest,
    principal: Principal = Depends(get_principal),
    uc: CreateOffer = Depends(get_create_offer),
) -> OfferResponse:
    """Restored in Phase 1G with seller identity resolved from membership.

    The 503 this replaces stood while `seller_id` had no honest source: the
    earlier `seller_id=str(current.id)` made the authenticated USER the seller,
    and the target model forbids that. Merchant (1C) and membership resolution
    (1F) now supply the missing link, so ownership comes from a SellerMember
    grant — the caller names the seller, the Principal proves they may act for
    it, and a nomination that is not in their memberships is refused.
    """
    cmd = CreateOfferCommand(
        # str(...) matches the DTO's declared type; the value is the UUID parsed
        # by the schema, so nothing unvalidated reaches the use case.
        seller_id=str(body.seller_id),
        variant_id=str(body.variant_id),
        condition=body.condition,
        currency=body.currency,
        amount=body.amount,
        scale=body.scale,
        compare_at_amount=body.compare_at_amount,
    )
    return OfferResponse.from_view(await uc.execute(cmd, principal))


@router.post("/offers/{offer_id}/activate", response_model=OfferResponse)
async def activate_offer(
    offer_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    uc: ActivateOffer = Depends(get_activate_offer),
) -> OfferResponse:
    return OfferResponse.from_view(await uc.execute(offer_id, principal))


@router.post("/offers/{offer_id}/suspend", response_model=OfferResponse)
async def suspend_offer(
    offer_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    uc: SuspendOffer = Depends(get_suspend_offer),
) -> OfferResponse:
    return OfferResponse.from_view(await uc.execute(offer_id, principal))


@router.patch("/offers/{offer_id}/price", response_model=OfferResponse)
async def change_offer_price(
    offer_id: uuid.UUID,
    body: ChangeOfferPriceRequest,
    principal: Principal = Depends(get_principal),
    uc: ChangeOfferPrice = Depends(get_change_price),
) -> OfferResponse:
    cmd = ChangeOfferPriceCommand(
        amount=body.amount,
        currency=body.currency,
        scale=body.scale,
        compare_at_amount=body.compare_at_amount,
    )
    return OfferResponse.from_view(await uc.execute(offer_id, cmd, principal))
