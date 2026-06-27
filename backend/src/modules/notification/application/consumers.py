"""Notification consumers (event handlers).

UserRegisteredHandler reacts to identity.UserRegistered by queuing a welcome
notification. Contract (per the relay design):
  * runs INSIDE the relay's transaction/session — never opens its own tx/UoW
  * idempotency is enforced by the dispatcher's inbox (handler.name + key)
  * does NOT touch the outbox / publish events itself
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from src.modules.notification.domain.entities import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from src.shared_kernel.infrastructure.outbox.contracts import IncomingEvent


def _default_repo_factory(session: Any):
    # Lazy import keeps this module DB-free for unit tests; the SQL repo (which
    # imports the engine) is only loaded when actually running against Postgres.
    from src.modules.notification.infrastructure.repository import SqlNotificationRepository

    return SqlNotificationRepository(session)


class UserRegisteredHandler:
    name = "notification.on_user_registered"        # stable inbox identity
    event_type = "identity.UserRegistered"          # subscription

    def __init__(self, repo_factory: Callable[[Any], Any] | None = None) -> None:
        self._repo_factory = repo_factory or _default_repo_factory

    async def handle(self, session: Any, event: IncomingEvent) -> None:
        user_id = uuid.UUID(event.payload["user_id"])   # opaque ref, not PII
        repo = self._repo_factory(session)
        notification = Notification.create(
            user_id=user_id,
            channel=NotificationChannel.SMS,
            template="welcome",
            payload={
                "source_event_id": str(event.event_id),
                "correlation_id": str(event.correlation_id) if event.correlation_id else None,
            },
        )
        assert notification.status == NotificationStatus.QUEUED
        await repo.add(notification)
        # No commit, no outbox write — the relay owns the transaction, the
        # dispatcher records the inbox row in the same savepoint.
