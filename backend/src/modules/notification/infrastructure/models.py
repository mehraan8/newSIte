"""ORM mapping for the Notification context.

Reuses the `notifications` table created in migration 0008 — no new migration.
"""

from __future__ import annotations

from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared_kernel.infrastructure.database import Base


class NotificationModel(Base):
    __tablename__ = "notifications"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    template: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default=text("'queued'"))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
