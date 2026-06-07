"""Request-id correlation + locale propagation middleware."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.config.settings import settings


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        locale = request.headers.get("Accept-Language", settings.default_locale)
        request.state.request_id = request_id
        request.state.locale = locale

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        # The storefront is RTL/Persian by default; advertise it.
        response.headers["Content-Language"] = settings.default_locale
        return response
