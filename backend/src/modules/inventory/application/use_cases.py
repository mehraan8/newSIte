"""Inventory use cases.

Concurrency: each operation is a load→mutate→optimistic-save inside a UoW; on a
ConcurrencyConflictError the whole UoW is retried with a fresh read. Idempotency:
keyed by reservation_id + reservation status (safe under retries and replays).
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import timedelta
from typing import Awaitable, Callable, TypeVar

from src.modules.inventory.application.dtos import (
    ReceiveStockCommand,
    ReservationView,
    ReserveStockCommand,
    StockView,
)
from src.modules.inventory.application.ports import SellingGateway
from src.modules.inventory.application.unit_of_work import InventoryUnitOfWork
from src.modules.inventory.domain.entities import (
    InventoryItem,
    Reservation,
    ReservationStatus,
)
from src.modules.inventory.domain.exceptions import InventoryDomainError
from src.shared_kernel.application.errors import (
    ConcurrencyConflictError,
    ConflictError,
    NotFoundError,
)
from src.shared_kernel.domain.base import utcnow

logger = logging.getLogger(__name__)

UowFactory = Callable[[], InventoryUnitOfWork]
T = TypeVar("T")


# Retry pacing. Module-level so tests can shrink it without touching call sites.
RETRY_BASE_DELAY_SECONDS = 0.02
RETRY_MAX_DELAY_SECONDS = 0.5


async def _with_retry(op: Callable[[], Awaitable[T]], attempts: int = 4) -> T:
    """Re-runs the whole UoW (fresh session, fresh read, fresh decision) on an
    optimistic-lock conflict. Backoff is exponential with full jitter: without it
    N contending writers retry in lockstep and keep colliding."""
    for i in range(attempts):
        try:
            return await op()
        except ConcurrencyConflictError:
            if i == attempts - 1:
                raise
            ceiling = min(RETRY_BASE_DELAY_SECONDS * (2 ** i), RETRY_MAX_DELAY_SECONDS)
            await asyncio.sleep(random.uniform(0, ceiling))
    raise AssertionError("unreachable")


class ReceiveStock:
    def __init__(self, uow_factory: UowFactory, selling: SellingGateway) -> None:
        self._uow_factory = uow_factory
        self._selling = selling

    async def execute(self, cmd: ReceiveStockCommand) -> StockView:
        offer_id = uuid.UUID(cmd.offer_id)
        if not await self._selling.offer_exists(offer_id):
            raise NotFoundError("آفر موردنظر یافت نشد.")

        async def op() -> StockView:
            uow = self._uow_factory()
            async with uow:
                item = await uow.inventory.get_item(offer_id, cmd.location)
                if item is None:
                    item = InventoryItem.create(offer_id=offer_id, location=cmd.location)
                    item.receive(cmd.quantity)
                    await uow.inventory.add_item(item)
                else:
                    item.receive(cmd.quantity)
                    await uow.inventory.save_item(item)
                await uow.commit()
            return StockView.from_domain(item)

        return await _with_retry(op)


class ReserveStock:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, cmd: ReserveStockCommand) -> ReservationView:
        reservation_id = uuid.UUID(cmd.reservation_id)
        offer_id = uuid.UUID(cmd.offer_id)

        async def op() -> ReservationView:
            uow = self._uow_factory()
            async with uow:
                # Idempotency: a known reservation id is a no-op.
                existing = await uow.inventory.get_reservation(reservation_id)
                if existing is not None:
                    return ReservationView.from_domain(existing)

                item = await uow.inventory.get_item(offer_id, cmd.location)
                if item is None:
                    raise NotFoundError("موجودی برای این آفر/محل ثبت نشده است.")
                try:
                    item.reserve(cmd.quantity, reservation_id)
                except InventoryDomainError as exc:
                    raise ConflictError(exc.message_fa) from exc

                reservation = Reservation(
                    id=reservation_id, offer_id=offer_id, location=cmd.location,
                    quantity=cmd.quantity, status=ReservationStatus.PENDING,
                    expires_at=utcnow() + timedelta(seconds=cmd.ttl_seconds),
                )
                await uow.inventory.save_item(item)          # optimistic
                await uow.inventory.add_reservation(reservation)  # unique id backstop
                await uow.commit()
                return ReservationView.from_domain(reservation)

        return await _with_retry(op)


class CommitReservation:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: uuid.UUID) -> ReservationView:
        async def op() -> ReservationView:
            uow = self._uow_factory()
            async with uow:
                res = await uow.inventory.get_reservation(reservation_id)
                if res is None:
                    raise NotFoundError("رزرو یافت نشد.")
                if res.status == ReservationStatus.COMMITTED:
                    return ReservationView.from_domain(res)         # idempotent
                if res.status != ReservationStatus.PENDING:
                    raise ConflictError("رزرو در وضعیت قابل تأیید نیست.")

                item = await uow.inventory.get_item(res.offer_id, res.location)
                if item is None:
                    raise ConflictError("موجودی مربوط به این رزرو یافت نشد.")
                try:
                    item.commit(res.quantity, res.id)
                    res.commit()
                except InventoryDomainError as exc:
                    raise ConflictError(exc.message_fa) from exc
                await uow.inventory.save_item(item)
                await uow.inventory.save_reservation(
                    res, expected_status=ReservationStatus.PENDING
                )
                await uow.commit()
                return ReservationView.from_domain(res)

        return await _with_retry(op)


class CommitForOrder:
    """Saga participant for ordering.InventoryCommitRequested.

    Differs from CommitReservation in its failure protocol only: a reservation
    that is no longer holdable (expired or released out from under the order) is
    a business outcome the saga MUST hear about, not an exception thrown into the
    relay. Raising here would retry 10x and dead-letter, leaving a PAID order
    whose stock was never committed and nobody informed.
    """

    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: uuid.UUID) -> None:
        async def op() -> None:
            uow = self._uow_factory()
            async with uow:
                res = await uow.inventory.get_reservation(reservation_id)
                if res is None or res.status == ReservationStatus.COMMITTED:
                    return  # unknown or already done — idempotent
                item = await uow.inventory.get_item(res.offer_id, res.location)

                if res.status != ReservationStatus.PENDING or item is None:
                    # The hold is gone. Reply with a failure fact so the saga can
                    # compensate instead of silently dead-lettering.
                    carrier = item or InventoryItem.create(
                        offer_id=res.offer_id, location=res.location
                    )
                    carrier.fail_reservation(
                        reservation_id, res.quantity, f"reservation_{res.status}"
                    )
                    if item is None:
                        await uow.inventory.add_item(carrier)
                    else:
                        uow.track(carrier)
                    await uow.commit()
                    return

                try:
                    item.commit(res.quantity, res.id)
                    res.commit()
                except InventoryDomainError:
                    # Counters disagree with the reservation (I8 drift). Raising
                    # here would dead-letter a PAID order with nobody informed,
                    # so reply with a fact and leave the counters untouched.
                    logger.exception(
                        "inventory commit rejected for reservation %s", reservation_id
                    )
                    fresh = await uow.inventory.get_item(res.offer_id, res.location)
                    fresh.fail_reservation(reservation_id, res.quantity, "commit_rejected")
                    uow.track(fresh)
                    await uow.commit()
                    return

                await uow.inventory.save_item(item)
                await uow.inventory.save_reservation(
                    res, expected_status=ReservationStatus.PENDING
                )
                await uow.commit()

        await _with_retry(op)


class ReleaseReservation:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, reservation_id: uuid.UUID, *, reason: str = "manual") -> ReservationView:
        async def op() -> ReservationView:
            uow = self._uow_factory()
            async with uow:
                res = await uow.inventory.get_reservation(reservation_id)
                if res is None:
                    raise NotFoundError("رزرو یافت نشد.")
                if res.status in (ReservationStatus.RELEASED, ReservationStatus.EXPIRED):
                    return ReservationView.from_domain(res)         # idempotent
                if res.status == ReservationStatus.COMMITTED:
                    raise ConflictError("رزرو تأییدشده قابل آزادسازی نیست.")

                item = await uow.inventory.get_item(res.offer_id, res.location)
                if item is None:
                    raise ConflictError("موجودی مربوط به این رزرو یافت نشد.")
                try:
                    item.release(res.quantity, res.id, reason=reason)
                    res.release()
                except InventoryDomainError as exc:
                    raise ConflictError(exc.message_fa) from exc
                await uow.inventory.save_item(item)
                await uow.inventory.save_reservation(
                    res, expected_status=ReservationStatus.PENDING
                )
                await uow.commit()
                return ReservationView.from_domain(res)

        return await _with_retry(op)


class ReserveForOrder:
    """Saga participant: reserve for one order line, emitting InventoryReserved on
    success or InventoryReservationFailed on shortage — never raises on shortage,
    so the saga always gets a reply. Idempotent by reservation_id."""

    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self, *, reservation_id: uuid.UUID, offer_id: uuid.UUID, location: str,
        quantity: int, ttl_seconds: int = 900,
    ) -> None:
        async def op() -> None:
            uow = self._uow_factory()
            async with uow:
                if await uow.inventory.get_reservation(reservation_id) is not None:
                    return  # idempotent — already handled
                item = await uow.inventory.get_item(offer_id, location)
                if item is not None and item.available >= quantity:
                    item.reserve(quantity, reservation_id)
                    reservation = Reservation(
                        id=reservation_id, offer_id=offer_id, location=location,
                        quantity=quantity, status=ReservationStatus.PENDING,
                        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                    )
                    await uow.inventory.save_item(item)
                    await uow.inventory.add_reservation(reservation)
                else:
                    # No stock (or none recorded) -> emit a failure reply. When no
                    # row exists we persist a zero-stock one rather than emitting
                    # from a phantom aggregate: the outbox stamps aggregate_id from
                    # the carrier, and that id must resolve to a real row.
                    if item is None:
                        carrier = InventoryItem.create(offer_id=offer_id, location=location)
                        carrier.fail_reservation(reservation_id, quantity, "insufficient_stock")
                        await uow.inventory.add_item(carrier)
                    else:
                        item.fail_reservation(reservation_id, quantity, "insufficient_stock")
                        uow.track(item)
                await uow.commit()

        await _with_retry(op)


class ExpireReservations:
    """Background sweeper: expire pending reservations past their TTL.

    One UoW (and one FOR UPDATE SKIP LOCKED claim) PER reservation, not one for
    the batch. Batching would mean a single optimistic conflict on one hot offer
    rolls back every other reservation swept in that pass, and a second sweeper
    instance would contend on the whole batch instead of skipping locked rows.
    """

    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, *, limit: int = 100) -> int:
        """Returns how many reservations were expired. Ends the pass on a
        reservation that stays contended through the retry budget: rows are
        claimed oldest-first, so retrying it in-loop would spin on the same row
        forever. Work already committed in this pass survives — that is the point
        of the per-reservation transaction. The next scheduled pass retries it,
        and a persistent gap between due and swept counts is the signal to alarm
        on."""
        swept = 0
        poisoned: set[uuid.UUID] = set()
        for _ in range(limit):
            try:
                outcome = await _with_retry(lambda: self._sweep_one(poisoned))
            except ConcurrencyConflictError:
                break
            if outcome is None:
                break
            swept += int(outcome)
        return swept

    async def _sweep_one(self, poisoned: set[uuid.UUID]) -> bool | None:
        """Claims, expires and commits exactly one reservation. The claim lock is
        held for this UoW only, so processing must happen inside it.

        Returns True (expired), False (skipped a row the domain refused) or None
        (nothing left due). Rows are claimed oldest-first, so a row that can
        never be expired would otherwise be re-claimed forever and block every
        younger one; `poisoned` steps over it for the rest of the pass.
        """
        uow = self._uow_factory()
        async with uow:
            due = await uow.inventory.find_expired_pending(
                utcnow(), limit=len(poisoned) + 1
            )
            candidates = [r for r in due if r.id not in poisoned]
            if not candidates:
                return None
            res = candidates[0]

            item = await uow.inventory.get_item(res.offer_id, res.location)
            if item is not None:
                try:
                    item.expire_reservation(res.quantity, res.id)
                except InventoryDomainError:
                    # Counters cannot absorb this release (I8 drift). Commit
                    # nothing — a half-expired reservation is worse than a stuck
                    # one — and leave it PENDING for the reconciliation job.
                    logger.exception(
                        "reservation %s cannot be expired; skipping for this pass", res.id
                    )
                    poisoned.add(res.id)
                    return False
                await uow.inventory.save_item(item)

            res.expire()
            await uow.inventory.save_reservation(
                res, expected_status=ReservationStatus.PENDING
            )
            await uow.commit()
            return True
