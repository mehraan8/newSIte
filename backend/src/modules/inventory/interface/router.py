"""Inventory endpoints (admin / internal).

These are commands; future Ordering will call ReserveStock/Commit/Release
in-process via the use cases rather than over HTTP.

Authentication is enforced here rather than delegated to a gateway: no gateway
exists in this deployment, and these routes mutate sellable stock.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from src.modules.identity.interface.dependencies import get_current_user

from src.modules.inventory.application.dtos import (
    ReceiveStockCommand,
    ReserveStockCommand,
)
from src.modules.inventory.application.use_cases import (
    CommitReservation,
    ReceiveStock,
    ReleaseReservation,
    ReserveStock,
)
from src.modules.inventory.infrastructure.selling_acl import SqlSellingGateway
from src.modules.inventory.infrastructure.unit_of_work import SqlAlchemyInventoryUnitOfWork

router = APIRouter(
    prefix="/inventory",
    tags=["inventory"],
    # Authentication is mandatory for every route on this router.
    dependencies=[Depends(get_current_user)],
)


def get_receive() -> ReceiveStock:
    return ReceiveStock(uow_factory=SqlAlchemyInventoryUnitOfWork, selling=SqlSellingGateway())


def get_reserve() -> ReserveStock:
    return ReserveStock(uow_factory=SqlAlchemyInventoryUnitOfWork)


def get_commit() -> CommitReservation:
    return CommitReservation(uow_factory=SqlAlchemyInventoryUnitOfWork)


def get_release() -> ReleaseReservation:
    return ReleaseReservation(uow_factory=SqlAlchemyInventoryUnitOfWork)


# UUID-typed (not str): a malformed id is now a 422 from the schema instead of a
# ValueError raised by uuid.UUID() deep in the use case, which surfaced as a 500.
# The wire format is unchanged — a UUID string still parses.
class ReceiveStockBody(BaseModel):
    offer_id: uuid.UUID
    location: str = Field(default="default", min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=1_000_000)


class ReserveStockBody(BaseModel):
    reservation_id: uuid.UUID
    offer_id: uuid.UUID
    location: str = Field(default="default", min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=1_000_000)
    # A non-positive TTL created a hold that was already expired on arrival.
    ttl_seconds: int = Field(default=900, gt=0, le=86_400)


@router.post("/stock", status_code=status.HTTP_200_OK)
async def receive_stock(body: ReceiveStockBody, uc: ReceiveStock = Depends(get_receive)):
    return await uc.execute(
        ReceiveStockCommand(
            offer_id=str(body.offer_id), location=body.location, quantity=body.quantity
        )
    )


@router.post("/reservations", status_code=status.HTTP_201_CREATED)
async def reserve(body: ReserveStockBody, uc: ReserveStock = Depends(get_reserve)):
    return await uc.execute(
        ReserveStockCommand(
            reservation_id=str(body.reservation_id), offer_id=str(body.offer_id),
            location=body.location, quantity=body.quantity, ttl_seconds=body.ttl_seconds,
        )
    )


@router.post("/reservations/{reservation_id}/commit")
async def commit(reservation_id: uuid.UUID, uc: CommitReservation = Depends(get_commit)):
    return await uc.execute(reservation_id)


@router.post("/reservations/{reservation_id}/release")
async def release(reservation_id: uuid.UUID, uc: ReleaseReservation = Depends(get_release)):
    return await uc.execute(reservation_id)
