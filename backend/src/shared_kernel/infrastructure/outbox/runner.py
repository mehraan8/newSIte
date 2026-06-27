"""Relay composition root + standalone entrypoint.

This is where handlers are registered and the delivery seam is chosen. Today it
wires the in-process publisher; switching to Kafka means replacing
`InProcessPublisher` with a `KafkaPublisher` here — the relay worker is untouched.

Run as its own process (separate from the API):
    python -m src.shared_kernel.infrastructure.outbox.runner
"""

from __future__ import annotations

import asyncio

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
    from src.modules.notification.application.consumers import UserRegisteredHandler

    registry.register(UserRegisteredHandler())
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
    asyncio.run(_main())
