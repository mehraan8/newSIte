"""Inventory endpoints (admin / internal; sit behind gateway RBAC).

These are commands; future Ordering will call ReserveStock/Commit/Release
in-process via the use cases rather than over HTTP.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

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

router = APIRouter(prefix="/inventory", tags=["inventory"])


def get_receive() -> ReceiveStock:
    return ReceiveStock(uow_factory=SqlAlchemyInventoryUnitOfWork, selling=SqlSellingGateway())


def get_reserve() -> ReserveStock:
    return ReserveStock(uow_factory=SqlAlchemyInventoryUnitOfWork)


def get_commit() -> CommitReservation:
    return CommitReservation(uow_factory=SqlAlchemyInventoryUnitOfWork)


def get_release() -> ReleaseReservation:
    return ReleaseReservation(uow_factory=SqlAlchemyInventoryUnitOfWork)


class ReceiveStockBody(BaseModel):
    offer_id: str
    location: str = "default"
    quantity: int


class ReserveStockBody(BaseModel):
    reservation_id: str
    offer_id: str
    location: str = "default"
    quantity: int
    ttl_seconds: int = 900


@router.post("/stock", status_code=status.HTTP_200_OK)
async def receive_stock(body: ReceiveStockBody, uc: ReceiveStock = Depends(get_receive)):
    return await uc.execute(
        ReceiveStockCommand(offer_id=body.offer_id, location=body.location, quantity=body.quantity)
    )


@router.post("/reservations", status_code=status.HTTP_201_CREATED)
async def reserve(body: ReserveStockBody, uc: ReserveStock = Depends(get_reserve)):
    return await uc.execute(
        ReserveStockCommand(
            reservation_id=body.reservation_id, offer_id=body.offer_id,
            location=body.location, quantity=body.quantity, ttl_seconds=body.ttl_seconds,
        )
    )


@router.post("/reservations/{reservation_id}/commit")
async def commit(reservation_id: uuid.UUID, uc: CommitReservation = Depends(get_commit)):
    return await uc.execute(reservation_id)


@router.post("/reservations/{reservation_id}/release")
async def release(reservation_id: uuid.UUID, uc: ReleaseReservation = Depends(get_release)):
    return await uc.execute(reservation_id)
