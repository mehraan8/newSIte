"""Centralized error → HTTP mapping.

Domain/application code raises AppError subclasses; this is the single place
they become HTTP responses (RFC 9457 Problem Details shape) with localized
Persian messages. The domain never imports FastAPI.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.shared_kernel.application.errors import AppError

logger = logging.getLogger("api.errors")


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
        return _problem(422, "validation_error", "ورودی نامعتبر است.", request, exc.errors())

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return _problem(500, "internal_error", "خطای داخلی سرور رخ داد.", request)
