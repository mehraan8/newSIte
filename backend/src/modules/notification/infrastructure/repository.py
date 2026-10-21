"""Notification persistence adapter — operates on the caller's session.

The handler runs inside the relay's transaction, so this repository must NOT
commit. It only adds/flushes on the provided session.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.notification.domain.entities import Notification
from src.modules.notification.infrastructure.models import NotificationModel


class SqlNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, notification: Notification) -> None:
        model = NotificationModel(
            user_id=notification.user_id,
            channel=notification.channel,
            template=notification.template,
            payload=notification.payload,
            status=notification.status,
        )
        self._s.add(model)
        await self._s.flush()      # assign the BIGINT id, no commit
        notification.id = model.id
