"""Pure (DB-free) helpers that turn a DomainEvent into outbox-ready data.

Kept import-light on purpose so it can be unit-tested without a database or
settings: it depends only on the domain base.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal

from src.shared_kernel.application.pii import assert_clean_payload
from src.shared_kernel.domain.base import DomainEvent


def qualified_event_type(event: DomainEvent) -> str:
    """`<context>.<EventName>` derived from the event's module path.

    e.g. UserRegistered in src.modules.identity.domain.entities -> "identity.UserRegistered".
    Approximates the ADR-0008 fully-qualified name without coupling events to it.
    """
    parts = type(event).__module__.split(".")
    context = "shared"
    if "modules" in parts:
        i = parts.index("modules")
        if i + 1 < len(parts):
            context = parts[i + 1]
    return f"{context}.{type(event).__name__}"


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


# Envelope fields live in dedicated outbox columns — they must NOT be duplicated
# inside the JSON payload, which holds only the domain-specific data.
_ENVELOPE_FIELDS = frozenset(
    {"event_id", "occurred_at", "aggregate_id", "aggregate_type", "idempotency_key", "pii_refs"}
)


def event_payload(event: DomainEvent) -> dict:
    """JSON-safe dict of the event's DOMAIN fields only (envelope excluded).

    Enforces the PII guard in code: a payload with raw personal data raises
    PiiLeakageError here, before it can ever reach the outbox.
    """
    raw = asdict(event) if is_dataclass(event) else dict(vars(event))
    domain_only = {k: v for k, v in raw.items() if k not in _ENVELOPE_FIELDS}
    payload = _jsonable(domain_only)
    assert_clean_payload(payload)
    return payload  # type: ignore[return-value]
