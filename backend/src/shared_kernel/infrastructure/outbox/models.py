"""Transactional outbox table (production envelope).

One row per domain event, written in the SAME transaction as the state change
that produced it (see SqlAlchemyUnitOfWork.commit). A separate relay later reads
unpublished rows and ships them to the broker — at-least-once, idempotent.

Idempotency-safe storage:
  * event_id is the PK            -> an event instance can never be stored twice
  * idempotency_key is UNIQUE     -> business-level dedup (defaults to event_id)

Dispatch-state columns (published_flag, retry_count, available_at, published_at,
last_error) belong to the relay, not the producer; the UoW writes the defaults.
"""

from __future__ import annotations

from typing import Optional

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared_kernel.infrastructure.database import Base


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    # --- identity / dedup ---
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)

    # --- envelope ---
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    event_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    causation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    region: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'global'"))
    pii_refs: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    # --- body ---
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # --- dispatch state (owned by the relay) ---
    published_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
        # Relay poll: only rows that are due and unpublished, oldest first.
        Index(
            "ix_outbox_ready",
            "available_at",
            postgresql_where=text("published_flag = false"),
        ),
        # Tracing: all events of one business flow.
        Index("ix_outbox_correlation", "correlation_id"),
    )


class ProcessedEvent(Base):
    """Consumer inbox — one row per (handler, event) successfully processed.

    Composite PK gives consumer-side dedup: a handler processes a given
    idempotency_key at most once.
    """

    __tablename__ = "processed_events"

    handler_name: Mapped[str] = mapped_column(String(120), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DeadLetterMessage(Base):
    """Poison events parked for inspection/replay (DLQ without a broker)."""

    __tablename__ = "dead_letter_messages"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    event_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    region: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dead_lettered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
