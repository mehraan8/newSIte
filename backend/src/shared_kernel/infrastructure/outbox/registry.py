"""In-process handler registry (DB-free).

Maps event_type -> ordered list of handlers. A registered handler is part of the
Published Language contract: it declares which event it consumes.
"""

from __future__ import annotations

from collections import defaultdict

from src.shared_kernel.infrastructure.outbox.contracts import EventHandler


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def register(self, handler: EventHandler) -> None:
        existing = self._handlers[handler.event_type]
        # Guard against double-registration of the same handler identity.
        if any(h.name == handler.name for h in existing):
            raise ValueError(
                f"handler '{handler.name}' already registered for '{handler.event_type}'"
            )
        existing.append(handler)

    def handlers_for(self, event_type: str) -> list[EventHandler]:
        return list(self._handlers.get(event_type, ()))

    def event_types(self) -> list[str]:
        return list(self._handlers.keys())
