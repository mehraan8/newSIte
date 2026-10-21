"""Notification domain model. Pure Python, same DDD rules as every context."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.shared_kernel.domain.base import AggregateRoot, DomainEvent


class NotificationStatus:
    """Lifecycle: QUEUED -> SENT | FAILED (matches the notifications table CHECK)."""

    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


class NotificationChannel:
    SMS = "sms"
    EMAIL = "email"
    PUSH = "push"


@dataclass(frozen=True)
class NotificationCreated(DomainEvent):
    # No raw PII here — only the channel/template and the aggregate linkage that
    # record_event stamps. Recipient resolution happens at send time.
    channel: str = ""
    template: str = ""


class Notification(AggregateRoot):
    def __init__(
        self,
        user_id: uuid.UUID,
        channel: str,
        template: str,
        payload: dict,
        *,
        id: int | None = None,
        status: str = NotificationStatus.QUEUED,
    ) -> None:
        super().__init__()
        self.id = id                       # assigned by the DB on persist
        self.user_id = user_id
        self.channel = channel
        self.template = template
        self.payload = payload
        self.status = status

    @classmethod
    def create(cls, user_id: uuid.UUID, channel: str, template: str, payload: dict) -> "Notification":
        n = cls(user_id, channel, template, payload, status=NotificationStatus.QUEUED)
        n.record_event(NotificationCreated(channel=channel, template=template))
        return n

    def mark_sent(self) -> None:
        if self.status != NotificationStatus.QUEUED:
            raise ValueError(f"cannot send a notification in status {self.status!r}")
        self.status = NotificationStatus.SENT

    def mark_failed(self) -> None:
        self.status = NotificationStatus.FAILED
