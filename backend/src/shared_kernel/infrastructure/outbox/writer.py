"""Stages domain events as fully-enriched outbox rows on the active session.

Called by the UoW during commit, BEFORE session.commit(), so rows land in the
same transaction as the state change. Enrichment combines:
  * intrinsic fields from the event (set by the aggregate / record_event)
  * ambient fields from EventContext (correlation/causation/tenant/region)
This function never commits and never publishes to a broker.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared_kernel.application.event_context import (
    EventContext,
    require_event_context,
)
from src.shared_kernel.application.pii import assert_clean_pii_refs
from src.shared_kernel.domain.base import AggregateRoot, DomainEvent
from src.shared_kernel.infrastructure.outbox.models import OutboxMessage
from src.shared_kernel.infrastructure.outbox.serialization import event_payload


def to_outbox_message(
    aggregate: AggregateRoot, event: DomainEvent, ctx: EventContext
) -> OutboxMessage:
    assert_clean_pii_refs(event.pii_refs)   # tokens only, never raw PII
    return OutboxMessage(
        event_id=event.event_id,
        idempotency_key=event.idempotency_key or str(event.event_id),
        aggregate_id=str(event.aggregate_id or aggregate.id),
        aggregate_type=event.aggregate_type or type(aggregate).__name__,
        event_type=event.event_type,
        event_version=type(event).event_version,
        occurred_at=event.occurred_at,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        tenant_id=ctx.tenant_id,
        region=ctx.region,
        pii_refs=list(event.pii_refs),
        payload=event_payload(event),
        published_flag=False,
        retry_count=0,
        # available_at / created_at default to now() server-side.
    )


def write_events_to_outbox(
    session: AsyncSession,
    events: Iterable[tuple[AggregateRoot, DomainEvent]],
) -> int:
    """Add an enriched OutboxMessage per event to the session. Returns the count.

    Enforces context injection at the UoW boundary: if there are events to write,
    an EventContext MUST be present (raises EventContextError otherwise). This
    makes it impossible to publish an event without correlation/region/tenant.
    """
    events = list(events)
    if not events:
        return 0
    ctx = require_event_context()       # mandatory — cannot be forgotten
    rows = [to_outbox_message(aggregate, event, ctx) for aggregate, event in events]
    session.add_all(rows)
    return len(rows)
