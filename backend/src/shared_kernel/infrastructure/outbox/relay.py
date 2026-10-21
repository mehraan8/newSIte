"""Outbox Relay worker.

Drains `outbox_messages` and delivers each event via the Publisher. Owns:
  * batched, lock-safe claiming (FOR UPDATE SKIP LOCKED)
  * retry scheduling (backoff on available_at)
  * dead-lettering of poison events
  * the 'published' transition

It does NOT contain business logic (that's in handlers) and does NOT know about
any broker (that's behind the Publisher seam).

Transaction model per batch (one outer tx):
  1. SELECT ... WHERE not published AND available_at<=now
        ORDER BY available_at LIMIT n FOR UPDATE SKIP LOCKED   (claims rows)
  2. for each row: publisher.publish(session, event)
        - success -> mark published
        - handler failure -> bump retry_count, reschedule (backoff) or DLQ
  3. COMMIT  (handler DB writes + inbox + published mark together = atomic)
A crash before COMMIT rolls the whole batch back -> rows stay unpublished ->
re-claimed later. Locks release on commit/rollback. At-least-once, no partial.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared_kernel.domain.base import utcnow
from src.shared_kernel.infrastructure.database import SessionFactory
from src.shared_kernel.infrastructure.outbox.contracts import (
    DeadLetterStore,
    EventPublisher,
    IncomingEvent,
)
from src.shared_kernel.infrastructure.outbox.models import OutboxMessage
from src.shared_kernel.infrastructure.outbox.retry import Backoff, on_failure


def to_incoming(row: OutboxMessage) -> IncomingEvent:
    return IncomingEvent(
        event_id=row.event_id,
        idempotency_key=row.idempotency_key,
        event_type=row.event_type,
        event_version=row.event_version,
        aggregate_id=row.aggregate_id,
        aggregate_type=row.aggregate_type,
        occurred_at=row.occurred_at,
        payload=row.payload,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        tenant_id=row.tenant_id,
        region=row.region,
        pii_refs=tuple(row.pii_refs or ()),
    )


class OutboxRelayWorker:
    def __init__(
        self,
        publisher: EventPublisher,
        dlq: DeadLetterStore,
        *,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        batch_size: int = 100,
        max_retries: int = 10,
        backoff: Backoff | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._publisher = publisher
        self._dlq = dlq
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._backoff = backoff or Backoff()
        self._poll_interval = poll_interval

    async def process_batch(self) -> int:
        """Claim and process one batch. Returns rows seen (0 = queue idle)."""
        async with self._session_factory() as session:
            async with session.begin():  # one outer transaction per batch
                stmt = (
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.published_flag.is_(False),
                        OutboxMessage.available_at <= utcnow(),
                    )
                    .order_by(OutboxMessage.available_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)  # concurrency-safe claim
                )
                rows = list((await session.scalars(stmt)).all())
                for row in rows:
                    await self._process_row(session, row)
            return len(rows)

    async def _process_row(self, session: AsyncSession, row: OutboxMessage) -> None:
        event = to_incoming(row)
        try:
            await self._publisher.publish(session, event)
        except Exception as exc:  # noqa: BLE001 - relay must isolate any failure
            outcome = on_failure(row.retry_count, self._max_retries, self._backoff)
            row.retry_count = outcome.retry_count
            if outcome.dead_letter:
                await self._dlq.dead_letter(session, row, repr(exc))
                row.published_flag = True  # terminal: remove from the relay queue
                row.last_error = f"dead-lettered after {outcome.retry_count} attempts: {exc!r}"
            else:
                row.available_at = utcnow() + timedelta(seconds=outcome.delay_seconds or 0)
                row.last_error = repr(exc)
            return

        row.published_flag = True
        row.published_at = utcnow()
        row.last_error = None

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            processed = await self.process_batch()
            if processed == 0:
                await asyncio.sleep(self._poll_interval)
