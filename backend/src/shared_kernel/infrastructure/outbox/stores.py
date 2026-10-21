"""SQLAlchemy implementations of InboxStore and DeadLetterStore."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared_kernel.infrastructure.outbox.contracts import IncomingEvent
from src.shared_kernel.infrastructure.outbox.models import (
    DeadLetterMessage,
    OutboxMessage,
    ProcessedEvent,
)


class SqlInboxStore:
    async def already_processed(
        self, session: AsyncSession, handler_name: str, idempotency_key: str
    ) -> bool:
        found = await session.scalar(
            select(ProcessedEvent.idempotency_key).where(
                ProcessedEvent.handler_name == handler_name,
                ProcessedEvent.idempotency_key == idempotency_key,
            )
        )
        return found is not None

    async def mark_processed(
        self, session: AsyncSession, handler_name: str, event: IncomingEvent
    ) -> None:
        session.add(
            ProcessedEvent(
                handler_name=handler_name,
                idempotency_key=event.idempotency_key,
                event_id=event.event_id,
                event_type=event.event_type,
            )
        )


class SqlDeadLetterStore:
    async def dead_letter(
        self, session: AsyncSession, row: OutboxMessage, error: str
    ) -> None:
        session.add(
            DeadLetterMessage(
                event_id=row.event_id,
                idempotency_key=row.idempotency_key,
                aggregate_id=row.aggregate_id,
                aggregate_type=row.aggregate_type,
                event_type=row.event_type,
                event_version=row.event_version,
                occurred_at=row.occurred_at,
                correlation_id=row.correlation_id,
                tenant_id=row.tenant_id,
                region=row.region,
                payload=row.payload,
                error=error[:4000],
                attempts=row.retry_count,
            )
        )
