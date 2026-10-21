"""SQLAlchemy adapter for the Order saga aggregate. Returns domain only; explicit
eager loading of lines; tracks the aggregate so saga events drain to the outbox."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.ordering.application.ports import IdempotencyRecord
from src.modules.ordering.domain.entities import Order, OrderLine
from src.modules.ordering.infrastructure.models import (
    OrderIdempotencyModel,
    OrderLineModel,
    OrderModel,
)
from src.shared_kernel.application.errors import ConcurrencyConflictError
from src.shared_kernel.application.unit_of_work import Tracker
from src.shared_kernel.domain.base import utcnow


def _to_domain(row: OrderModel) -> Order:
    lines = [
        OrderLine(
            offer_id=l.offer_id, location=l.location, quantity=l.quantity,
            unit_amount=l.unit_amount, reservation_id=l.reservation_id, status=l.status,
        )
        for l in row.lines
    ]
    return Order(
        id=row.id, customer_id=row.customer_id, region=row.region,
        currency=row.currency, lines=lines, status=row.status, version=row.version,
    )


class SqlOrderIdempotencyStore:
    """Operates on the caller's session — the key row commits with the order."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def find(
        self, customer_id: uuid.UUID, idempotency_key: str
    ) -> IdempotencyRecord | None:
        row = await self._s.get(OrderIdempotencyModel, (customer_id, idempotency_key))
        if row is None:
            return None
        return IdempotencyRecord(
            order_id=row.order_id, request_fingerprint=row.request_fingerprint
        )

    async def remember(
        self,
        customer_id: uuid.UUID,
        idempotency_key: str,
        order_id: uuid.UUID,
        request_fingerprint: str,
    ) -> None:
        self._s.add(
            OrderIdempotencyModel(
                customer_id=customer_id, idempotency_key=idempotency_key,
                order_id=order_id, request_fingerprint=request_fingerprint,
            )
        )
        try:
            await self._s.flush()
        except IntegrityError as exc:
            # Concurrent duplicate won the race. The use case retries and its
            # find() then returns the winner's order.
            raise ConcurrencyConflictError("idempotency key already used") from exc


class SqlOrderRepository:
    def __init__(self, session: AsyncSession, tracker: Tracker | None = None) -> None:
        self._s = session
        self._tracker = tracker

    def _track(self, order: Order) -> None:
        if self._tracker is not None:
            self._tracker.track(order)

    async def _load(self, order_id: uuid.UUID) -> OrderModel | None:
        return await self._s.scalar(
            select(OrderModel).options(selectinload(OrderModel.lines)).where(OrderModel.id == order_id)
        )

    async def get(self, order_id: uuid.UUID) -> Order | None:
        row = await self._load(order_id)
        if row is None:
            return None
        order = _to_domain(row)
        self._track(order)
        return order

    async def find_by_reservation_id(self, reservation_id: uuid.UUID) -> Order | None:
        order_id = await self._s.scalar(
            select(OrderLineModel.order_id).where(OrderLineModel.reservation_id == reservation_id)
        )
        if order_id is None:
            return None
        return await self.get(order_id)

    async def add(self, order: Order) -> None:
        row = OrderModel(
            id=order.id, customer_id=order.customer_id, region=order.region,
            currency=order.currency, status=order.status, total_amount=order.total_amount,
        )
        row.lines = [
            OrderLineModel(
                offer_id=ln.offer_id, location=ln.location, quantity=ln.quantity,
                unit_amount=ln.unit_amount, reservation_id=ln.reservation_id, status=ln.status,
            )
            for ln in order.lines
        ]
        self._s.add(row)
        await self._s.flush()
        self._track(order)

    async def save(self, order: Order) -> None:
        # Version-guarded compare-and-set on the aggregate ROOT. Losing the race
        # means another reaction already advanced this order, so our in-memory
        # decision was made against stale state and must be recomputed — the use
        # case retries the whole UoW with a fresh read.
        result = await self._s.execute(
            update(OrderModel)
            .where(OrderModel.id == order.id, OrderModel.version == order.version)
            .values(
                status=order.status,
                # The column is a denormalisation of Order.total_amount, which is
                # derived from the lines. Re-materialise it on every write so the
                # stored value can never drift from the aggregate that owns it.
                total_amount=order.total_amount,
                version=order.version + 1,
                updated_at=utcnow(),
            )
        )
        if result.rowcount == 0:
            raise ConcurrencyConflictError("order version conflict")
        order.version += 1

        # Winning the root CAS makes us the only writer of this aggregate's
        # lines, so these updates need no guard of their own.
        row = await self._load(order.id)
        if row is None:                                  # deleted under us
            raise ConcurrencyConflictError("order disappeared during update")
        by_rid = {str(l.reservation_id): l for l in row.lines}
        for ln in order.lines:
            model_line = by_rid.get(str(ln.reservation_id))
            if model_line is not None:
                model_line.status = ln.status
        await self._s.flush()
        self._track(order)
