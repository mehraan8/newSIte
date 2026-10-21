"""Centralized error → HTTP mapping.

Domain/application code raises AppError subclasses; this is the single place
they become HTTP responses (RFC 9457 Problem Details shape) with localized
Persian messages. The domain never imports FastAPI.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.shared_kernel.application.errors import AppError
from src.shared_kernel.domain.value_objects import (
    InvalidMobileError,
    InvalidMoneyError,
    InvalidSlugError,
)

logger = logging.getLogger("api.errors")

# Value objects reject malformed user input by raising these. They are
# ValueError subclasses, so before this mapping existed they fell through to the
# catch-all and surfaced as 500s. Only these three are mapped — NOT ValueError at
# large: PiiLeakageError is also a ValueError, and a PII leak is a server defect
# that must stay a 500 rather than be reported to the caller as a bad request.
_INPUT_VALUE_ERRORS = (InvalidMobileError, InvalidMoneyError, InvalidSlugError)

# asyncpg/psycopg constraint violations that are caused by the request, not by a
# server defect. Anything not listed here stays a 500.
_DB_ERROR_MAP = {
    "ForeignKeyViolationError": (422, "ارجاع نامعتبر است: مورد ارجاع‌شده وجود ندارد."),
    "UniqueViolationError": (409, "این مورد از قبل وجود دارد."),
    "CheckViolationError": (422, "مقدار ارسالی با محدودیت‌های داده سازگار نیست."),
    "NotNullViolationError": (422, "یکی از فیلدهای الزامی خالی است."),
    "StringDataRightTruncationError": (422, "طول یکی از فیلدها بیش از حد مجاز است."),
    "NumericValueOutOfRangeError": (422, "مقدار عددی خارج از محدودهٔ مجاز است."),
}


def _db_cause(exc: BaseException) -> tuple[int, str] | None:
    """Walks to the driver-level error and classifies it, if it is user-caused."""
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None), exc):
        name = type(candidate).__name__ if candidate is not None else ""
        if name in _DB_ERROR_MAP:
            return _DB_ERROR_MAP[name]
    return None


def _problem(status: int, code: str, message_fa: str, request: Request, detail=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "type": f"https://errors.example.com/{code}",
            "title": code,
            "status": status,
            "message_fa": message_fa,
            "detail": detail,
            "request_id": getattr(request.state, "request_id", None),
        },
        headers={"Content-Language": "fa-IR"},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return _problem(exc.http_status, exc.code, exc.message_fa, request, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # jsonable_encoder: for a non-JSON body the error entries carry the raw
        # `bytes` input, which JSONResponse cannot serialize — the 422 handler
        # itself then raised and the caller got a 500.
        return _problem(
            422, "validation_error", "ورودی نامعتبر است.", request, jsonable_encoder(exc.errors())
        )

    async def _invalid_value(request: Request, exc: ValueError):
        return _problem(422, "validation_error", "ورودی نامعتبر است.", request, str(exc))

    # Registered one class at a time: Starlette's add_exception_handler asserts
    # issubclass(...) and so rejects a tuple of classes.
    for _exc_type in _INPUT_VALUE_ERRORS:
        app.add_exception_handler(_exc_type, _invalid_value)

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):
        mapped = _db_cause(exc)
        if mapped is None:
            logger.exception("unmapped integrity error: %s", exc)
            return _problem(500, "internal_error", "خطای داخلی سرور رخ داد.", request)
        status, message_fa = mapped
        code = "conflict" if status == 409 else "validation_error"
        return _problem(status, code, message_fa, request)

    @app.exception_handler(DBAPIError)
    async def _dbapi(request: Request, exc: DBAPIError):
        # Covers data errors that are not IntegrityError (e.g. value too long for
        # varchar(n), numeric out of range).
        mapped = _db_cause(exc)
        if mapped is None:
            logger.exception("unmapped database error: %s", exc)
            return _problem(500, "internal_error", "خطای داخلی سرور رخ داد.", request)
        status, message_fa = mapped
        return _problem(status, "validation_error", message_fa, request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return _problem(500, "internal_error", "خطای داخلی سرور رخ داد.", request)
