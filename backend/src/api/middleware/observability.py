"""Request-id correlation + locale propagation middleware.

Also seeds the ambient EventContext so domain events written by the UoW during
this request inherit correlation_id / region / tenant_id automatically.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.config.settings import settings
from src.shared_kernel.application.event_context import (
    EventContext,
    reset_event_context,
    set_event_context,
)


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        locale = request.headers.get("Accept-Language", settings.default_locale)
        request.state.request_id = request_id
        request.state.locale = locale

        # Correlation = the request id (parsed to UUID, else a fresh one).
        token = set_event_context(
            EventContext(
                correlation_id=_as_uuid(request_id) or uuid.uuid4(),
                tenant_id=_as_uuid(request.headers.get("X-Tenant-ID")),
                region=request.headers.get("X-Region", "global"),
            )
        )
        try:
            response = await call_next(request)
        finally:
            reset_event_context(token)

        response.headers["X-Request-ID"] = request_id
        # The storefront is RTL/Persian by default; advertise it.
        response.headers["Content-Language"] = settings.default_locale
        return response
