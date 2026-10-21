"""Ordering saga consumers — the process manager's event subscriptions.

Reaction handlers advance the Order saga from participant REPLY events. The
payment step is stubbed: a PaymentRequested consumer auto-approves by driving
HandlePaymentSucceeded. When the real Payments context lands, register its
PaymentSucceeded/PaymentFailed handlers instead and drop the stub.

Each handler opens its own Ordering UoW; idempotency comes from the order/line
state machine, so at-least-once delivery and relay replays are safe.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from src.shared_kernel.infrastructure.outbox.contracts import (
    IncomingEvent,
    TransactionMode,
)


def _ordering_uow_factory():
    from src.modules.ordering.infrastructure.unit_of_work import SqlAlchemyOrderingUnitOfWork

    return SqlAlchemyOrderingUnitOfWork


class OnInventoryReserved:
    name = "ordering.on_inventory_reserved"
    event_type = "inventory.InventoryReserved"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self, uow_factory: Callable | None = None) -> None:
        self._uf = uow_factory or _ordering_uow_factory()

    async def handle(self, session: Any, event: IncomingEvent) -> None:
        from src.modules.ordering.application.use_cases import HandleInventoryReserved

        await HandleInventoryReserved(self._uf).execute(uuid.UUID(event.payload["reservation_id"]))


class OnInventoryReservationFailed:
    name = "ordering.on_inventory_reservation_failed"
    event_type = "inventory.InventoryReservationFailed"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self, uow_factory: Callable | None = None) -> None:
        self._uf = uow_factory or _ordering_uow_factory()

    async def handle(self, session: Any, event: IncomingEvent) -> None:
        from src.modules.ordering.application.use_cases import HandleInventoryReservationFailed

        await HandleInventoryReservationFailed(self._uf).execute(
            uuid.UUID(event.payload["reservation_id"]),
            reason=event.payload.get("reason", "insufficient_stock"),
        )


class OnInventoryExpired:
    name = "ordering.on_inventory_expired"
    event_type = "inventory.InventoryExpired"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self, uow_factory: Callable | None = None) -> None:
        self._uf = uow_factory or _ordering_uow_factory()

    async def handle(self, session: Any, event: IncomingEvent) -> None:
        from src.modules.ordering.application.use_cases import HandleInventoryExpired

        await HandleInventoryExpired(self._uf).execute(
            uuid.UUID(event.payload["reservation_id"])
        )


class PaymentStubOnPaymentRequested:
    """STUB: auto-approves payment. Replace with the Payments context later."""

    name = "ordering.payment_stub_on_payment_requested"
    event_type = "ordering.PaymentRequested"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self, uow_factory: Callable | None = None) -> None:
        self._uf = uow_factory or _ordering_uow_factory()

    async def handle(self, session: Any, event: IncomingEvent) -> None:
        from src.modules.ordering.application.use_cases import HandlePaymentSucceeded

        await HandlePaymentSucceeded(self._uf).execute(uuid.UUID(event.payload["order_id"]))
