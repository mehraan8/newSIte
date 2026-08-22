"""Response security headers.

An API, not a web app, so this is a deliberately small set — headers that do
nothing for a JSON endpoint are noise that obscures the ones that matter.

What is here and why:

  * `X-Content-Type-Options: nosniff` — stops a browser from re-interpreting a
    JSON response as HTML or script. This is the one that matters most: an error
    body echoing user input, served as `text/html` by a sniffing browser, is
    reflected XSS.
  * `Referrer-Policy: no-referrer` — API URLs carry ids in the path; none of
    that should leak to a third-party host in a Referer header.
  * `X-Frame-Options: DENY` — cheap, and correct for endpoints that must never
    be framed. Kept even though JSON is not framable, because `/docs` is.
  * `Cross-Origin-Resource-Policy: same-site` — blocks opportunistic
    cross-origin reads of API responses by other sites.
  * `Cache-Control: no-store` on authenticated responses — an authorization
    decision is made per request, so a cached copy of one caller's data is a
    copy served to the next.
  * `Strict-Transport-Security` — only outside development, and only meaningful
    over HTTPS. Sending it from a local http:// dev server would pin the
    developer's browser to HTTPS for localhost.

Deliberately NOT here: a Content-Security-Policy. A useful CSP constrains a
document's script sources; these endpoints return JSON, and a blanket policy
would break FastAPI's own `/docs` page while protecting nothing.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.config.settings import settings

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-site",
}

HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for name, value in STATIC_HEADERS.items():
            response.headers.setdefault(name, value)

        if not settings.is_development:
            response.headers.setdefault("Strict-Transport-Security", HSTS)

        # Anything produced for a credentialed request is caller-specific by
        # definition — it was shaped by that caller's permissions.
        if request.headers.get("Authorization"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
