"""Checkout endpoint. PlaceOrder returns immediately (PENDING); the saga then
drives reservation → payment → commit asynchronously via events."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict, Field

from src.modules.identity.interface.dependencies import CurrentUser, get_current_user
from src.modules.ordering.application.dtos import (
    OrderLineInput,
    PlaceOrderCommand,
)
from src.modules.ordering.application.use_cases import CancelOrder, PlaceOrder
from src.modules.ordering.infrastructure.buybox_acl import SqlBuyBoxPricingGateway
from src.modules.ordering.infrastructure.unit_of_work import SqlAlchemyOrderingUnitOfWork
from src.shared_kernel.application.errors import ForbiddenError, NotFoundError
from src.shared_kernel.infrastructure.database import get_session
from src.modules.ordering.infrastructure.repository import SqlOrderRepository
from src.modules.ordering.application.dtos import OrderView

router = APIRouter(prefix="/orders", tags=["ordering"])


# extra="forbid" on both: a client that still sends `unit_amount` or
# `customer_id` gets a 422 instead of having it silently ignored. Silent
# ignoring hides an attempted bypass; rejecting surfaces it.
# Abuse limits. These bound the request, not the business rules: a legitimate
# basket is far below them. Without a ceiling a single call could open hundreds of
# reservations, drain a variant's stock and fail every concurrent shopper's
# checkout — observed in the audit, where one 500-line request exhausted stock and
# failed six unrelated orders.
MAX_ORDER_LINES = 50
MAX_LINE_QUANTITY = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 255


class LineBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # UUID-typed: a malformed offer_id is a 422 from the schema instead of a
    # ValueError inside the use case (previously a 500). Wire format unchanged.
    offer_id: uuid.UUID
    location: str = Field(default="default", min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=MAX_LINE_QUANTITY)


class CheckoutBody(BaseModel):
    """No customer_id: it comes from the bearer token. No unit_amount: it comes
    from the pricing ACL. Neither is negotiable by the caller."""

    model_config = ConfigDict(extra="forbid")

    region: str = Field(default="global", min_length=1, max_length=64)
    currency: str = Field(default="IRR", min_length=3, max_length=3)
    lines: list[LineBody] = Field(min_length=1, max_length=MAX_ORDER_LINES)


def get_cancel_order() -> CancelOrder:
    return CancelOrder(uow_factory=SqlAlchemyOrderingUnitOfWork)


def get_place_order() -> PlaceOrder:
    return PlaceOrder(
        uow_factory=SqlAlchemyOrderingUnitOfWork,
        pricing=SqlBuyBoxPricingGateway(),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def checkout(
    body: CheckoutBody,
    uc: PlaceOrder = Depends(get_place_order),
    current: CurrentUser = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", max_length=MAX_IDEMPOTENCY_KEY_LENGTH
    ),
):
    """Send an Idempotency-Key to make a retry safe: the same key returns the
    original order instead of placing a second one.

    Opt-in rather than mandatory so existing callers are not broken. It SHOULD
    become required before public launch — without it a network retry silently
    double-holds stock and, once Payments is real, double-charges.
    """
    cmd = PlaceOrderCommand(
        # Authenticated principal — NOT a request field.
        customer_id=str(current.id),
        region=body.region,
        currency=body.currency,
        lines=tuple(
            OrderLineInput(offer_id=str(li.offer_id), location=li.location,
                           quantity=li.quantity)
            for li in body.lines
        ),
        idempotency_key=idempotency_key,
    )
    return await uc.execute(cmd)


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: uuid.UUID,
    uc: CancelOrder = Depends(get_cancel_order),
    current: CurrentUser = Depends(get_current_user),
):
    """Cancels a PENDING or RESERVED order and hands back any stock it holds.

    Idempotent: cancelling an already CANCELLED/FAILED order returns it
    unchanged. A PAID order is rejected with 409 — reversing a paid order is a
    refund/return, a different process that Payments will own.
    """
    return await uc.execute(order_id, customer_id=current.id)


@router.get("/{order_id}")
async def get_order(
    order_id: uuid.UUID,
    session=Depends(get_session),
    current: CurrentUser = Depends(get_current_user),
):
    order = await SqlOrderRepository(session).get(order_id)
    if order is None:
        raise NotFoundError("سفارش یافت نشد.")
    # Object-level authorization: an order id is guessable enough that owning the
    # id must not equal being allowed to read it.
    if order.customer_id != current.id:
        raise ForbiddenError("دسترسی به این سفارش مجاز نیست.")
    return OrderView.from_domain(order)
