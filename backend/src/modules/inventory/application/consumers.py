"""Inventory saga participant consumers.

Inventory performs its own mutations in reaction to Ordering COMMAND events —
the orchestrator never calls Inventory synchronously. Each consumer wraps an
existing idempotent Inventory use case.

TransactionMode.OWN_UNIT_OF_WORK: these handlers must EMIT events (the saga's
replies), and only a UoW drains aggregates into the outbox atomically with the
state change — so they cannot use the relay session. The trade is that their
commit lands before the inbox row, making delivery at-least-once. Safety comes
from each use case being idempotent on reservation_id, NOT from the inbox.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from src.shared_kernel.infrastructure.outbox.contracts import TransactionMode


def _uow_factory():
    from src.modules.inventory.infrastructure.unit_of_work import SqlAlchemyInventoryUnitOfWork

    return SqlAlchemyInventoryUnitOfWork


class OnOrderReservationRequested:
    name = "inventory.on_order_reservation_requested"
    event_type = "ordering.OrderReservationRequested"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self, uow_factory: Callable | None = None) -> None:
        self._uf = uow_factory or _uow_factory()

    async def handle(self, session: Any, event: Any) -> None:
        from src.modules.inventory.application.use_cases import ReserveForOrder

        uc = ReserveForOrder(self._uf)
        for line in event.payload.get("lines", ()):
            await uc.execute(
                reservation_id=uuid.UUID(line["reservation_id"]),
                offer_id=uuid.UUID(line["offer_id"]),
                location=line["location"],
                quantity=int(line["quantity"]),
            )


class OnInventoryCommitRequested:
    name = "inventory.on_commit_requested"
    event_type = "ordering.InventoryCommitRequested"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self, uow_factory: Callable | None = None) -> None:
        self._uf = uow_factory or _uow_factory()

    async def handle(self, session: Any, event: Any) -> None:
        # CommitForOrder, not CommitReservation: a saga participant replies with a
        # fact instead of raising into the relay (see its docstring).
        from src.modules.inventory.application.use_cases import CommitForOrder

        uc = CommitForOrder(self._uf)
        for rid in event.payload.get("reservation_ids", ()):
            await uc.execute(uuid.UUID(rid))


class OnInventoryReleaseRequested:
    name = "inventory.on_release_requested"
    event_type = "ordering.InventoryReleaseRequested"
    transaction_mode = TransactionMode.OWN_UNIT_OF_WORK

    def __init__(self, uow_factory: Callable | None = None) -> None:
        self._uf = uow_factory or _uow_factory()

    async def handle(self, session: Any, event: Any) -> None:
        # ReleaseForOrder, not ReleaseReservation: the operator-facing class now
        # requires a Principal, and a saga has no caller to authorize. Same
        # split as reserve and commit above.
        from src.modules.inventory.application.use_cases import ReleaseForOrder

        uc = ReleaseForOrder(self._uf)
        reason = event.payload.get("reason", "compensation")
        for rid in event.payload.get("reservation_ids", ()):
            await uc.execute(uuid.UUID(rid), reason=reason)
