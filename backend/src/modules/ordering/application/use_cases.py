"""Ordering use cases.

PlaceOrder only creates the order and emits events (OrderPlaced +
OrderReservationRequested) — NO synchronous cross-context calls. The saga then
advances via reaction use cases, each triggered by a participant reply event and
run in its own UoW (idempotent via the order/line state machine).
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Awaitable, Callable, TypeVar

from src.modules.ordering.application.dtos import OrderView, PlaceOrderCommand
from src.modules.ordering.application.ports import OfferPricingGateway
from src.modules.ordering.application.unit_of_work import OrderingUnitOfWork
from src.modules.ordering.domain.entities import Order, OrderLine
from src.modules.ordering.domain.exceptions import OrderingDomainError
from src.shared_kernel.application.errors import (
    ConcurrencyConflictError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)

UowFactory = Callable[[], OrderingUnitOfWork]
T = TypeVar("T")

# Retry pacing. Module-level so tests can shrink it without touching call sites.
RETRY_BASE_DELAY_SECONDS = 0.02
RETRY_MAX_DELAY_SECONDS = 0.5


async def _with_retry(op: Callable[[], Awaitable[T]], attempts: int = 4) -> T:
    """Re-runs the whole UoW on an optimistic-lock conflict, so the saga
    reaction re-decides against fresh state rather than replaying a stale
    decision. Backoff is exponential with full jitter.

    Mirrors Inventory's identical helper. Duplicated rather than shared: a
    bounded-context's application layer does not import another's. Converge into
    shared_kernel only if a third context needs it.
    """
    for i in range(attempts):
        try:
            return await op()
        except ConcurrencyConflictError:
            if i == attempts - 1:
                raise
            ceiling = min(RETRY_BASE_DELAY_SECONDS * (2 ** i), RETRY_MAX_DELAY_SECONDS)
            await asyncio.sleep(random.uniform(0, ceiling))
    raise AssertionError("unreachable")


class PlaceOrder:
    """Creates the order and emits the saga's first commands. No synchronous
    cross-context call except the read-only pricing ACL, which resolves the
    snapshot BEFORE any state is written."""

    def __init__(self, uow_factory: UowFactory, pricing: OfferPricingGateway) -> None:
        self._uow_factory = uow_factory
        self._pricing = pricing

    async def execute(self, cmd: PlaceOrderCommand) -> OrderView:
        customer_id = uuid.UUID(cmd.customer_id)
        # Priced before the transaction opens. A replay re-prices needlessly but
        # harmlessly — the stored order keeps its ORIGINAL snapshot.
        lines = await self._resolve_lines(cmd)

        async def op() -> OrderView:
            uow = self._uow_factory()
            async with uow:
                if cmd.idempotency_key:
                    replay = await self._replay(uow, customer_id, cmd)
                    if replay is not None:
                        return replay

                order = Order.place(
                    customer_id=customer_id, region=cmd.region,
                    currency=cmd.currency, lines=lines,
                )
                await uow.orders.add(order)
                if cmd.idempotency_key:
                    # Same transaction as the order: a key is never recorded for
                    # an order that did not commit, and never missing for one
                    # that did. Unique violation -> conflict -> retry -> replay.
                    await uow.idempotency.remember(
                        customer_id, cmd.idempotency_key, order.id, cmd.fingerprint()
                    )
                await uow.commit()
                return OrderView.from_domain(order)

        return await _with_retry(op)

    async def _replay(self, uow, customer_id: uuid.UUID, cmd: PlaceOrderCommand):
        """Returns the original order for a re-used key, or None if the key is new."""
        record = await uow.idempotency.find(customer_id, cmd.idempotency_key)
        if record is None:
            return None
        if record.request_fingerprint != cmd.fingerprint():
            # Same key, different order. Silently returning the first one would
            # hide a client bug and ship the wrong goods.
            raise ConflictError("این کلید یکتایی قبلاً برای سفارش دیگری استفاده شده است.")
        order = await uow.orders.get(record.order_id)
        if order is None:                       # order deleted under the key
            raise ConflictError("سفارش مرتبط با این کلید یکتایی یافت نشد.")
        return OrderView.from_domain(order)

    async def _resolve_lines(self, cmd: PlaceOrderCommand) -> list[OrderLine]:
        """Prices every line from the read model. Any price the caller sent was
        already dropped at the interface boundary; this is where the real one is
        established and frozen onto the line."""
        lines: list[OrderLine] = []
        scales: set[int] = set()

        for li in cmd.lines:
            if li.quantity <= 0:
                raise ValidationError("تعداد باید بزرگ‌تر از صفر باشد.")

            offer_id = uuid.UUID(li.offer_id)
            snapshot = await self._pricing.price_for(offer_id, cmd.region)
            if snapshot is None:
                raise NotFoundError("آفر موردنظر در این منطقه یافت نشد.")
            if not snapshot.is_active or snapshot.amount is None:
                raise ConflictError("این آفر در حال حاضر قابل سفارش نیست.")
            if snapshot.currency != cmd.currency:
                raise ValidationError("ارز آفر با ارز سفارش هم‌خوان نیست.")

            scales.add(snapshot.scale)
            lines.append(
                OrderLine(
                    offer_id=offer_id, location=li.location,
                    quantity=li.quantity, unit_amount=snapshot.amount,
                )
            )

        # total_amount sums raw minor units, so mixed scales would silently
        # mis-total. Same currency should mean same scale; refuse if it does not.
        if len(scales) > 1:
            raise ValidationError("دقت اعشاری قیمت آفرها یکسان نیست.")
        return lines


class CancelOrder:
    """Customer-initiated cancellation.

    A valid operation: the domain already models it end-to-end (CANCELLED is in
    the status CHECK, `cancel()` compensates reserved lines and rejects a PAID
    order). It was simply unreachable — no endpoint called it.

    Authorization lives HERE, not only in the router: cancelling is a state
    change, and the rule "only the owner may cancel" is part of the operation
    rather than a transport concern.

    Lines still in flight at Inventory are safe: when their reserve lands, the
    order is CANCELLED (an ABANDONED state) and the hold is handed straight back.
    """

    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, order_id: uuid.UUID, *, customer_id: uuid.UUID) -> OrderView:
        async def op() -> OrderView:
            uow = self._uow_factory()
            async with uow:
                order = await uow.orders.get(order_id)
                if order is None:
                    raise NotFoundError("سفارش یافت نشد.")
                if order.customer_id != customer_id:
                    raise ForbiddenError("دسترسی به این سفارش مجاز نیست.")
                try:
                    order.cancel()          # idempotent for CANCELLED / FAILED
                except OrderingDomainError as exc:
                    raise ConflictError(exc.message_fa) from exc
                await uow.orders.save(order)
                await uow.commit()
                return OrderView.from_domain(order)

        return await _with_retry(op)


# --- saga reaction use cases (one UoW each; idempotent via state guards) ----
class HandleInventoryReserved:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: uuid.UUID) -> None:
        async def op() -> None:
            uow = self._uow_factory()
            async with uow:
                order = await uow.orders.find_by_reservation_id(reservation_id)
                if order is None:
                    return
                order.on_line_reserved(reservation_id)
                await uow.orders.save(order)
                await uow.commit()

        await _with_retry(op)


class HandleInventoryReservationFailed:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: uuid.UUID, *, reason: str = "insufficient_stock") -> None:
        async def op() -> None:
            uow = self._uow_factory()
            async with uow:
                order = await uow.orders.find_by_reservation_id(reservation_id)
                if order is None:
                    return
                order.on_line_reservation_failed(reservation_id, reason=reason)
                await uow.orders.save(order)
                await uow.commit()

        await _with_retry(op)


class HandleInventoryExpired:
    """Reaction to inventory.InventoryExpired. Without this subscription a lapsed
    hold is invisible to the saga: the order stays RESERVED, payment succeeds, and
    the commit fails against a reservation whose stock is already gone."""

    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: uuid.UUID) -> None:
        async def op() -> None:
            uow = self._uow_factory()
            async with uow:
                order = await uow.orders.find_by_reservation_id(reservation_id)
                if order is None:
                    return
                order.on_line_reservation_expired(reservation_id)
                await uow.orders.save(order)
                await uow.commit()

        await _with_retry(op)


class HandlePaymentSucceeded:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, order_id: uuid.UUID) -> None:
        async def op() -> None:
            uow = self._uow_factory()
            async with uow:
                order = await uow.orders.get(order_id)
                if order is None:
                    return
                order.on_payment_succeeded()
                await uow.orders.save(order)
                await uow.commit()

        await _with_retry(op)


class HandlePaymentFailed:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, order_id: uuid.UUID, *, reason: str = "payment_failed") -> None:
        async def op() -> None:
            uow = self._uow_factory()
            async with uow:
                order = await uow.orders.get(order_id)
                if order is None:
                    return
                order.on_payment_failed(reason=reason)
                await uow.orders.save(order)
                await uow.commit()

        await _with_retry(op)
