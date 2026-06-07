"""Application-level error taxonomy.

Bounded contexts raise these (or subclasses). The API layer maps them to HTTP
responses + localized Persian messages in one place — domain/application code
never imports FastAPI.
"""

from __future__ import annotations


class AppError(Exception):
    """Base for all expected application errors."""

    code: str = "app_error"
    http_status: int = 400
    message_fa: str = "خطایی رخ داد."

    def __init__(self, message_fa: str | None = None, *, detail: str | None = None) -> None:
        self.message_fa = message_fa or self.message_fa
        self.detail = detail
        super().__init__(self.message_fa)


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422
    message_fa = "ورودی نامعتبر است."


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    message_fa = "موردی یافت نشد."


class UnauthorizedError(AppError):
    code = "unauthorized"
    http_status = 401
    message_fa = "احراز هویت ناموفق بود."


class ForbiddenError(AppError):
    code = "forbidden"
    http_status = 403
    message_fa = "اجازه دسترسی ندارید."


class ConflictError(AppError):
    code = "conflict"
    http_status = 409
    message_fa = "تعارض در داده‌ها."


class RateLimitedError(AppError):
    code = "rate_limited"
    http_status = 429
    message_fa = "تعداد درخواست‌ها بیش از حد مجاز است. لطفاً بعداً تلاش کنید."
