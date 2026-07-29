"""Reservation TTL sweeper worker.

    python -m src.modules.inventory.infrastructure.expiry_worker

ExpireReservations existed but nothing invoked it: no endpoint, no scheduler, no
relay hook. Reservations therefore never expired, so held stock was never
returned and inventory.InventoryExpired was never emitted — which left the
ADR-0009 D1 oversell path open even after the event and its subscriber landed.

Runs OUTSIDE any HTTP request, so it must inject its own EventContext:
write_events_to_outbox requires one, and without it every sweep that actually
expires something would raise EventContextError at commit.
"""

from __future__ import annotations

import asyncio
import logging

from src.modules.inventory.application.use_cases import ExpireReservations
from src.modules.inventory.infrastructure.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from src.shared_kernel.application.event_context import system_event_context

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30.0
BATCH_LIMIT = 100


async def sweep_once(*, limit: int = BATCH_LIMIT, region: str = "global") -> int:
    """One pass. Returns the number of reservations expired.

    The context spans the pass, so every reservation expired together shares a
    correlation_id — enough to trace a sweep, coarser than one flow per
    reservation. Revisit if per-reservation tracing is needed.
    """
    with system_event_context(region=region):
        return await ExpireReservations(SqlAlchemyInventoryUnitOfWork).execute(limit=limit)


async def run_forever(stop: asyncio.Event | None = None) -> None:
    """A pass that raises must not kill the worker: expiry stopping is exactly
    the condition that reopens the oversell path this sweeper exists to close.
    Mirrors the relay's per-row isolation."""
    while stop is None or not stop.is_set():
        try:
            expired = await sweep_once()
        except Exception:  # noqa: BLE001 - isolate any pass failure
            logger.exception("reservation expiry sweep failed; retrying next tick")
            expired = 0
        if expired == 0:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    # A long-lived worker with no logging config emits only WARNING+ through
    # logging.lastResort, untimestamped — a swept-count of zero and a crash loop
    # look identical in `docker logs`.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    asyncio.run(run_forever())
