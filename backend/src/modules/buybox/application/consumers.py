"""BuyBox event handlers — one per consumed Selling event.

Each runs inside the relay's transaction; the dispatcher's inbox makes them
idempotent ((handler_name, idempotency_key)). They build the projector over a
session-bound repository (lazy import keeps this module DB-free for tests).
"""

from __future__ import annotations

from typing import Any, Callable

from src.modules.buybox.application.projector import BuyBoxProjector
from src.shared_kernel.infrastructure.outbox.contracts import IncomingEvent, TransactionMode


def _default_repo_factory(session: Any):
    from src.modules.buybox.infrastructure.repository import SqlBuyBoxRepository

    return SqlBuyBoxRepository(session)


class _BuyBoxHandler:
    name: str = ""
    event_type: str = ""
    projector_method: str = ""
    # Projects through the relay session -> atomic with the inbox row.
    transaction_mode = TransactionMode.RELAY_SESSION

    def __init__(self, repo_factory: Callable[[Any], Any] | None = None) -> None:
        self._repo_factory = repo_factory or _default_repo_factory

    async def _projector(self, session: Any) -> BuyBoxProjector:
        return BuyBoxProjector(self._repo_factory(session))

    async def handle(self, session: Any, event: IncomingEvent) -> None:
        projector = await self._projector(session)
        await getattr(projector, self.projector_method)(event)


class OnOfferCreated(_BuyBoxHandler):
    name = "buybox.on_offer_created"
    event_type = "selling.OfferCreated"
    projector_method = "on_offer_created"


class OnOfferActivated(_BuyBoxHandler):
    name = "buybox.on_offer_activated"
    event_type = "selling.OfferActivated"
    projector_method = "on_offer_activated"


class OnOfferPriceChanged(_BuyBoxHandler):
    name = "buybox.on_offer_price_changed"
    event_type = "selling.OfferPriceChanged"
    projector_method = "on_offer_price_changed"


class OnOfferSuspended(_BuyBoxHandler):
    """Without this the projection keeps a suspended offer winning forever."""

    name = "buybox.on_offer_suspended"
    event_type = "selling.OfferSuspended"
    projector_method = "on_offer_suspended"


# Single source of truth: the relay registry, the rebuild replay and the
# projection tests all derive their wiring from this tuple, so adding a handler
# can never leave one of them behind.
BUYBOX_HANDLERS: tuple[type[_BuyBoxHandler], ...] = (
    OnOfferCreated,
    OnOfferActivated,
    OnOfferPriceChanged,
    OnOfferSuspended,
)
BUYBOX_HANDLER_NAMES = tuple(h.name for h in BUYBOX_HANDLERS)
CONSUMED_EVENT_TYPES = tuple(h.event_type for h in BUYBOX_HANDLERS)
PROJECTOR_DISPATCH = {h.event_type: h.projector_method for h in BUYBOX_HANDLERS}
