"""Replay-based rebuild of the BuyBox projection.

Because the projection is fully derived and idempotent, it can be dropped and
rebuilt from the durable event log (outbox_messages) at any time:

    python -m src.modules.buybox.infrastructure.rebuild

Steps (one transaction):
  1. clear buy_box_variant_state + buy_box_offer
  2. clear the BuyBox handlers' inbox rows (so live relay re-delivery is consistent)
  3. replay all consumed Selling events in (occurred_at, created_at) order
The relay's at-least-once + inbox guarantees live and rebuilt paths converge.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.modules.buybox.application.consumers import (
    BUYBOX_HANDLER_NAMES,
    CONSUMED_EVENT_TYPES,
    PROJECTOR_DISPATCH,
)
from src.modules.buybox.application.projector import BuyBoxProjector
from src.modules.buybox.infrastructure.models import (
    BuyBoxOfferModel,
    BuyBoxVariantStateModel,
)
from src.modules.buybox.infrastructure.repository import SqlBuyBoxRepository
from src.shared_kernel.infrastructure.database import SessionFactory
from src.shared_kernel.infrastructure.outbox.models import OutboxMessage, ProcessedEvent
from src.shared_kernel.infrastructure.outbox.relay import to_incoming


async def rebuild_buybox(session_factory: async_sessionmaker[AsyncSession] = SessionFactory) -> int:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(BuyBoxVariantStateModel))
            await session.execute(delete(BuyBoxOfferModel))
            await session.execute(
                delete(ProcessedEvent).where(
                    ProcessedEvent.handler_name.in_(BUYBOX_HANDLER_NAMES)
                )
            )

            projector = BuyBoxProjector(SqlBuyBoxRepository(session))
            rows = await session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.event_type.in_(CONSUMED_EVENT_TYPES))
                .order_by(OutboxMessage.occurred_at, OutboxMessage.created_at)
            )
            count = 0
            for row in rows.all():
                event = to_incoming(row)
                await getattr(projector, PROJECTOR_DISPATCH[event.event_type])(event)
                count += 1
        return count


if __name__ == "__main__":
    n = asyncio.run(rebuild_buybox())
    print(f"buybox rebuilt from {n} events")
