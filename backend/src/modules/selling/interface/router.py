"""Selling endpoints (seller-facing).

Authentication is enforced here rather than delegated to a gateway: no gateway
exists in this deployment, and these routes create offers and set prices.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from src.modules.identity.interface.dependencies import CurrentUser, get_current_user
from src.modules.selling.application.dtos import (
    ChangeOfferPriceCommand,
    CreateOfferCommand,
)
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
async def create_offer(
    body: CreateOfferRequest,
    uc: CreateOffer = Depends(get_create_offer),
    current: CurrentUser = Depends(get_current_user),
) -> OfferResponse:
    cmd = CreateOfferCommand(
        # Authenticated principal — NOT a request field. A body-supplied
        # seller_id is rejected by the schema (extra="forbid").
        seller_id=str(current.id),
        variant_id=str(body.variant_id),
        condition=body.condition,
        currency=body.currency,
        amount=body.amount,
        scale=body.scale,
        compare_at_amount=body.compare_at_amount,
    )
    return OfferResponse.from_view(await uc.execute(cmd))


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
