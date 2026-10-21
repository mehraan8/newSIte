"""ADR-0009 invariant I8 — reconciliation audit.

    I8:  SUM(quantity) WHERE status='pending' AND (offer_id, location) = X
           ==  inventory_stock.reserved  for X

I8 is the seam created by keeping Reservation outside the InventoryItem
aggregate. No aggregate boundary enforces it: it holds only because every
lifecycle operation writes both rows inside one UoW. This job is the audit that
proves it still holds, and it is the single most valuable alarm in the context.

    python -m src.modules.inventory.infrastructure.reconciliation

READ-ONLY ON PURPOSE. It reports drift and never repairs it. Repair means
choosing which side is authoritative, and choosing wrong in the
`reserved < pending` direction hands the same stock to two shoppers — the exact
failure this context exists to prevent. Drift is a human decision with the
evidence in front of them, not something a cron job silently rewrites.

Exit code 1 when drift is found, so any scheduler treats it as a failed run.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.shared_kernel.infrastructure.database import SessionFactory

logger = logging.getLogger(__name__)

# FULL OUTER JOIN so both directions surface: a stock row with no reservations
# and reserved > 0, and pending reservations with no stock row at all.
# The grouped subquery is what ix_resv_offer_location_status supports.
_I8_SQL = text(
    """
    SELECT COALESCE(s.offer_id, r.offer_id)   AS offer_id,
           COALESCE(s.location, r.location)   AS location,
           COALESCE(s.reserved, 0)            AS reserved_counter,
           COALESCE(r.pending_total, 0)       AS pending_total
      FROM inventory_stock s
      FULL OUTER JOIN (
            SELECT offer_id, location, SUM(quantity) AS pending_total
              FROM inventory_reservation
             WHERE status = 'pending'
             GROUP BY offer_id, location
      ) r ON r.offer_id = s.offer_id AND r.location = s.location
     WHERE COALESCE(s.reserved, 0) <> COALESCE(r.pending_total, 0)
     ORDER BY offer_id, location
    """
)


@dataclass(frozen=True)
class Drift:
    """One (offer_id, location) where the counter and the holds disagree."""

    offer_id: str
    location: str
    reserved_counter: int
    pending_total: int

    @property
    def delta(self) -> int:
        return self.reserved_counter - self.pending_total

    @property
    def oversellable(self) -> bool:
        """Counter under-counts live holds, so `available` is overstated and the
        same units can be promised twice. The dangerous direction."""
        return self.delta < 0

    def describe(self) -> str:
        direction = "OVERSELLABLE" if self.oversellable else "stranded stock"
        return (
            f"{direction}: offer={self.offer_id} location={self.location} "
            f"reserved={self.reserved_counter} pending_holds={self.pending_total} "
            f"delta={self.delta:+d}"
        )


async def find_drift(
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> list[Drift]:
    async with session_factory() as session:
        result = await session.execute(_I8_SQL)
        return [
            Drift(
                offer_id=str(row.offer_id),
                location=row.location,
                reserved_counter=int(row.reserved_counter),
                pending_total=int(row.pending_total),
            )
            for row in result.all()
        ]


def report(drift: list[Drift]) -> int:
    """Logs the audit outcome. Returns a process exit code."""
    if not drift:
        logger.info("I8 reconciliation: OK, no drift")
        return 0
    oversellable = [d for d in drift if d.oversellable]
    logger.error(
        "I8 reconciliation: %d drifted bucket(s), %d oversellable",
        len(drift), len(oversellable),
    )
    for d in drift:
        logger.error("  %s", d.describe())
    return 1


async def _main() -> int:
    return report(await find_drift())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    sys.exit(asyncio.run(_main()))
