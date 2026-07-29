"""Ordering domain — the checkout saga as a process-manager aggregate.

Order IS the saga: it holds the state machine and, as participant reply events
arrive, transitions and records the next COMMAND event (reserve/payment/commit)
or compensation (release). All transitions are idempotent (guarded by order/line
status) so the saga is safe under at-least-once delivery and relay replays.

No synchronous cross-context calls: the aggregate only records events.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.modules.ordering.domain.exceptions import InvalidOrderTransition
from src.shared_kernel.domain.base import AggregateRoot, DomainEvent, new_uuid


class OrderStatus:
    PENDING = "pending"
    RESERVED = "reserved"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ALL = frozenset({PENDING, RESERVED, PAID, FAILED, CANCELLED})
    # Terminal states in which the order no longer wants stock. A reservation
    # that lands now must be given back, not held. PAID is terminal too but is
    # NOT here: a paid order still needs every hold it has.
    ABANDONED = frozenset({FAILED, CANCELLED})


class LineStatus:
    PENDING = "pending"
    RESERVED = "reserved"
    FAILED = "failed"
    RELEASED = "released"
    COMMITTED = "committed"


# --- events (PII-free) -----------------------------------------------------
@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    region: str = "global"
    currency: str = ""
    total_amount: int = 0
    line_count: int = 0


@dataclass(frozen=True)
class OrderReservationRequested(DomainEvent):  # command -> Inventory
    order_id: uuid.UUID | None = None
    region: str = "global"
    lines: tuple = ()   # ({offer_id, location, quantity, reservation_id}, ...)


@dataclass(frozen=True)
class OrderReserved(DomainEvent):
    order_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PaymentRequested(DomainEvent):  # command -> Payment (stub)
    order_id: uuid.UUID | None = None
    amount: int = 0
    currency: str = ""


@dataclass(frozen=True)
class OrderPaid(DomainEvent):
    order_id: uuid.UUID | None = None


@dataclass(frozen=True)
class InventoryCommitRequested(DomainEvent):  # command -> Inventory
    order_id: uuid.UUID | None = None
    reservation_ids: tuple = ()


@dataclass(frozen=True)
class InventoryReleaseRequested(DomainEvent):  # compensation -> Inventory
    order_id: uuid.UUID | None = None
    reservation_ids: tuple = ()
    reason: str = "compensation"


@dataclass(frozen=True)
class OrderFailed(DomainEvent):
    order_id: uuid.UUID | None = None
    reason: str = ""


@dataclass(frozen=True)
class OrderCancelled(DomainEvent):
    order_id: uuid.UUID | None = None


# --- OrderLine entity ------------------------------------------------------
class OrderLine:
    def __init__(
        self,
        *,
        offer_id: uuid.UUID,
        location: str,
        quantity: int,
        unit_amount: int,
        reservation_id: uuid.UUID | None = None,
        status: str = LineStatus.PENDING,
    ) -> None:
        if quantity <= 0:
            raise InvalidOrderTransition("line quantity must be positive")
        self.offer_id = offer_id
        self.location = location
        self.quantity = quantity
        self.unit_amount = unit_amount
        self.reservation_id = reservation_id or new_uuid()
        self.status = status

    @property
    def line_total(self) -> int:
        return self.unit_amount * self.quantity


# --- Order aggregate (process manager) -------------------------------------
class Order(AggregateRoot):
    def __init__(
        self,
        *,
        id: uuid.UUID,
        customer_id: uuid.UUID,
        region: str,
        currency: str,
        lines: list[OrderLine],
        status: str = OrderStatus.PENDING,
        version: int = 0,
    ) -> None:
        super().__init__()
        if not lines:
            raise InvalidOrderTransition("an order needs at least one line")
        if status not in OrderStatus.ALL:
            raise InvalidOrderTransition(f"invalid status {status!r}")
        self.id = id
        self.customer_id = customer_id
        self.region = region
        self.currency = currency
        self.lines = lines
        self.status = status
        # Optimistic lock. Saga replies for different lines of one order arrive
        # concurrently; without a version guard the second writer silently
        # overwrites the first's line status (a lost update).
        self.version = version

    @property
    def total_amount(self) -> int:
        return sum(line.line_total for line in self.lines)

    # -- step 0: placement (emits OrderPlaced + reserve command) --
    @classmethod
    def place(
        cls,
        *,
        customer_id: uuid.UUID,
        region: str,
        currency: str,
        lines: list[OrderLine],
    ) -> "Order":
        order = cls(
            id=new_uuid(), customer_id=customer_id, region=region,
            currency=currency, lines=lines, status=OrderStatus.PENDING,
        )
        order.record_event(
            OrderPlaced(
                order_id=order.id, customer_id=customer_id, region=region,
                currency=currency, total_amount=order.total_amount, line_count=len(lines),
            )
        )
        order.record_event(
            OrderReservationRequested(
                order_id=order.id, region=region,
                lines=tuple(
                    {
                        "offer_id": str(line.offer_id),
                        "location": line.location,
                        "quantity": line.quantity,
                        "reservation_id": str(line.reservation_id),
                    }
                    for line in lines
                ),
            )
        )
        return order

    # -- step 1 reply: inventory reserved / failed --
    def on_line_reserved(self, reservation_id: uuid.UUID) -> None:
        line = self._line_by_reservation(reservation_id)
        if line is None or line.status != LineStatus.PENDING:
            return  # idempotent / not applicable

        if self.status in OrderStatus.ABANDONED:
            # The order gave up while this line's reserve was still in flight.
            # Marking it RESERVED would strand real stock on a dead order until
            # Inventory's TTL swept it. Hand the hold straight back instead.
            line.status = LineStatus.RELEASED
            self.record_event(
                InventoryReleaseRequested(
                    order_id=self.id,
                    reservation_ids=(str(reservation_id),),
                    reason="order_already_terminal",
                )
            )
            return

        line.status = LineStatus.RESERVED
        if self.status == OrderStatus.PENDING and all(
            ln.status == LineStatus.RESERVED for ln in self.lines
        ):
            self.status = OrderStatus.RESERVED
            self.record_event(OrderReserved(order_id=self.id))
            # step 3: trigger payment
            self.record_event(
                PaymentRequested(order_id=self.id, amount=self.total_amount, currency=self.currency)
            )

    def on_line_reservation_failed(self, reservation_id: uuid.UUID, reason: str = "insufficient_stock") -> None:
        line = self._line_by_reservation(reservation_id)
        if line is None or line.status not in (LineStatus.PENDING, LineStatus.RESERVED):
            return
        line.status = LineStatus.FAILED
        if self.status in (OrderStatus.PENDING, OrderStatus.RESERVED):
            self._fail(reason)

    def on_line_reservation_expired(self, reservation_id: uuid.UUID) -> None:
        """A hold lapsed before the order could use it. Same saga outcome as a
        shortage — fail and compensate the siblings — because the stock is gone
        either way and may already belong to another shopper."""
        self.on_line_reservation_failed(reservation_id, reason="reservation_expired")

    # -- step 3 reply: payment outcome --
    def on_payment_succeeded(self) -> None:
        if self.status == OrderStatus.PAID:
            return  # idempotent
        if self.status != OrderStatus.RESERVED:
            return  # late/out-of-order reply for a non-reserved order — ignore
        self.status = OrderStatus.PAID
        self.record_event(OrderPaid(order_id=self.id))
        committed = []
        for line in self.lines:
            if line.status == LineStatus.RESERVED:
                line.status = LineStatus.COMMITTED
                committed.append(str(line.reservation_id))
        if committed:
            self.record_event(
                InventoryCommitRequested(order_id=self.id, reservation_ids=tuple(committed))
            )

    def on_payment_failed(self, reason: str = "payment_failed") -> None:
        if self.status != OrderStatus.RESERVED:
            return  # idempotent / not applicable
        self._fail(reason)

    def cancel(self) -> None:
        if self.status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
            return
        if self.status == OrderStatus.PAID:
            raise InvalidOrderTransition("a paid order cannot be cancelled here")
        self._compensate_reserved()
        self.status = OrderStatus.CANCELLED
        self.record_event(OrderCancelled(order_id=self.id))

    # -- internals --
    def _fail(self, reason: str) -> None:
        self.status = OrderStatus.FAILED
        self.record_event(OrderFailed(order_id=self.id, reason=reason))
        self._compensate_reserved()

    def _compensate_reserved(self) -> None:
        to_release = []
        for line in self.lines:
            if line.status == LineStatus.RESERVED:
                line.status = LineStatus.RELEASED
                to_release.append(str(line.reservation_id))
        if to_release:
            self.record_event(
                InventoryReleaseRequested(order_id=self.id, reservation_ids=tuple(to_release))
            )

    def _line_by_reservation(self, reservation_id: uuid.UUID) -> OrderLine | None:
        rid = str(reservation_id)
        for line in self.lines:
            if str(line.reservation_id) == rid:
                return line
        return None
