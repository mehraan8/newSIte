"""Ambient event context — the source of envelope metadata the domain must not know.

Set per request (or per consumed message) at the edge; read by the UoW/outbox
writer when enriching events. Using a ContextVar means the UoW, buried deep in
the call stack, gets correlation/causation/tenant/region without threading them
through every function or leaking them into aggregates.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class EventContext:
    correlation_id: uuid.UUID | None = None   # ties all events in one business flow
    causation_id: uuid.UUID | None = None     # the command/event that caused this one
    tenant_id: uuid.UUID | None = None        # seller/marketplace scope (nullable)
    region: str = "global"                    # region-of-record


_EMPTY = EventContext()
_ctx: ContextVar[EventContext] = ContextVar("event_context", default=_EMPTY)


class EventContextError(RuntimeError):
    """Raised when events are published without an injected EventContext."""


def current_event_context() -> EventContext:
    return _ctx.get()


def set_event_context(ctx: EventContext) -> Token:
    return _ctx.set(ctx)


def reset_event_context(token: Token) -> None:
    _ctx.reset(token)


def require_event_context(ctx: EventContext | None = None) -> EventContext:
    """Return the active context or raise. correlation_id is the required marker
    that context was actually injected (the empty default has it as None)."""
    ctx = ctx if ctx is not None else current_event_context()
    if ctx.correlation_id is None:
        raise EventContextError(
            "no EventContext set: events cannot be published without context "
            "injection. Use the request middleware, or wrap producers in "
            "`with system_event_context(): ...` for background/system flows."
        )
    return ctx


@contextmanager
def system_event_context(*, region: str = "global", tenant_id: uuid.UUID | None = None) -> Iterator[EventContext]:
    """Inject a context for non-HTTP producers (relay, jobs, seeds, CLI)."""
    ctx = EventContext(correlation_id=uuid.uuid4(), region=region, tenant_id=tenant_id)
    token = set_event_context(ctx)
    try:
        yield ctx
    finally:
        reset_event_context(token)
