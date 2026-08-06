"""Relay composition root + standalone entrypoint.

This is where handlers are registered and the delivery seam is chosen. Today it
wires the in-process publisher; switching to Kafka means replacing
`InProcessPublisher` with a `KafkaPublisher` here — the relay worker is untouched.

Run as its own process (separate from the API):
    python -m src.shared_kernel.infrastructure.outbox.runner
"""

from __future__ import annotations

import asyncio
import logging

from src.shared_kernel.infrastructure.outbox.dispatcher import (
    EventDispatcher,
    InProcessPublisher,
)
from src.shared_kernel.infrastructure.outbox.registry import HandlerRegistry
from src.shared_kernel.infrastructure.outbox.relay import OutboxRelayWorker
from src.shared_kernel.infrastructure.outbox.retry import Backoff
from src.shared_kernel.infrastructure.outbox.stores import (
    SqlDeadLetterStore,
    SqlInboxStore,
)


def build_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    # --- consumer registrations (the Published Language wiring) ---
    # NotificationModel declares a FK to Identity's `users` table. SQLAlchemy
    # resolves that target from the shared MetaData at mapper-configuration time,
    # so Identity's models must be imported in this process or the first insert
    # fails with NoReferencedTableError. The API process imports them via the auth
    # routes; the relay runs standalone and does not.
    from src.modules.identity.infrastructure import models as _identity_models  # noqa: F401
    from src.modules.notification.application.consumers import UserRegisteredHandler
    from src.modules.buybox.application.consumers import BUYBOX_HANDLERS

    registry.register(UserRegisteredHandler())
    for buybox_handler in BUYBOX_HANDLERS:
        registry.register(buybox_handler())

    # --- checkout saga wiring (Ordering orchestrates via events) ---
    from src.modules.inventory.application.consumers import (
        OnInventoryCommitRequested,
        OnInventoryReleaseRequested,
        OnOrderReservationRequested,
    )
    from src.modules.ordering.interface.consumers import (
        OnInventoryExpired,
        OnInventoryReservationFailed,
        OnInventoryReserved,
        PaymentStubOnPaymentRequested,
    )

    # Inventory reacts to Ordering commands (performs its own mutations):
    registry.register(OnOrderReservationRequested())
    registry.register(OnInventoryCommitRequested())
    registry.register(OnInventoryReleaseRequested())
    # Ordering saga reacts to Inventory replies + (stub) payment:
    registry.register(OnInventoryReserved())
    registry.register(OnInventoryReservationFailed())
    registry.register(OnInventoryExpired())
    registry.register(PaymentStubOnPaymentRequested())
    return registry


def build_relay(registry: HandlerRegistry | None = None) -> OutboxRelayWorker:
    registry = registry or build_registry()
    dispatcher = EventDispatcher(registry, SqlInboxStore())
    publisher = InProcessPublisher(dispatcher)        # <- swap for KafkaPublisher later
    return OutboxRelayWorker(
        publisher=publisher,
        dlq=SqlDeadLetterStore(),
        batch_size=100,
        max_retries=10,
        backoff=Backoff(base_seconds=5, factor=2, max_seconds=3600),
        poll_interval=1.0,
    )


async def _main() -> None:
    await build_relay().run_forever()


if __name__ == "__main__":
    # See expiry_worker: without this the relay's failures are untimestamped and
    # anything below WARNING is dropped entirely.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    asyncio.run(_main())
