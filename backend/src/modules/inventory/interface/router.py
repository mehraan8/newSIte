"""Inventory endpoints (admin / internal).

These are commands; future Ordering will call ReserveStock/Commit/Release
in-process via the use cases rather than over HTTP.

Authentication is enforced here rather than delegated to a gateway: no gateway
exists in this deployment, and these routes mutate sellable stock.

AUTHENTICATION alone was not enough, and for a while that is all these routes
had. The security audit confirmed that any logged-in customer could inject stock
against another seller's offer, reserve that seller's stock, and commit or
release ANY reservation by guessing nothing more than its id.

Two different controls now apply, because the operations are different kinds:

  * `POST /stock` is SELLER-SCOPED. The stock belongs to the offer's seller, so
    ownership is resolved from the offer and checked in the use case — a router
    guard cannot do it, because the router has not loaded the offer.
  * reservations are PLATFORM/INTERNAL. In normal operation the Ordering saga
    creates and commits them in-process; the HTTP routes exist for back-office
    correction, so they demand a platform permission and no seller can reach
    them. Guarding them per-reservation would be the wrong model — a reservation
    is not a merchant-owned resource.

Phase 1H moved the authoritative decision into the USE CASES. The router guards
below stay as a coarse floor, but they are not what makes these safe: a router
guard protects a route, not an operation, and these use cases are also reachable
from consumers and tests.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from src.api.dependencies import get_principal
from src.api.guards import require_permission
from src.shared_kernel.application.permissions import Permission
from src.shared_kernel.application.security import Principal

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

# Authentication is mandatory for every route on this router — a floor, so a
# route added without its own guard still cannot be reached anonymously.
router = APIRouter(
    prefix="/inventory",
    tags=["inventory"],
    dependencies=[Depends(get_principal)],
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
async def receive_stock(
    body: ReceiveStockBody,
    principal: Principal = Depends(get_principal),
    uc: ReceiveStock = Depends(get_receive),
):
    """Seller-scoped: the use case resolves the offer's owner and checks the
    caller's membership (or platform `inventory.manage.any`) before touching a
    counter."""
    return await uc.execute(
        ReceiveStockCommand(
            offer_id=str(body.offer_id), location=body.location, quantity=body.quantity
        ),
        principal,
    )


@router.post(
    "/reservations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.INVENTORY_RESERVATIONS_CREATE))],
)
async def reserve(
    body: ReserveStockBody,
    principal: Principal = Depends(get_principal),
    uc: ReserveStock = Depends(get_reserve),
):
    return await uc.execute(
        ReserveStockCommand(
            reservation_id=str(body.reservation_id), offer_id=str(body.offer_id),
            location=body.location, quantity=body.quantity, ttl_seconds=body.ttl_seconds,
        ),
        principal,
    )


@router.post(
    "/reservations/{reservation_id}/commit",
    dependencies=[Depends(require_permission(Permission.INVENTORY_RESERVATIONS_COMMIT))],
)
async def commit(
    reservation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    uc: CommitReservation = Depends(get_commit),
):
    return await uc.execute(reservation_id, principal)


@router.post(
    "/reservations/{reservation_id}/release",
    dependencies=[Depends(require_permission(Permission.INVENTORY_RESERVATIONS_RELEASE))],
)
async def release(
    reservation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    uc: ReleaseReservation = Depends(get_release),
):
    return await uc.execute(reservation_id, principal)
