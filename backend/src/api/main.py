"""FastAPI composition root — wires middleware, error handlers, and routers.

Run: uvicorn src.api.main:app --reload   (from the backend/ directory)
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.middleware.error_handlers import register_error_handlers
from src.api.middleware.observability import RequestContextMiddleware
from src.api.middleware.security_headers import SecurityHeadersMiddleware
from src.api.v1.router import api_v1
from src.config.settings import settings
from src.shared_kernel.infrastructure.redis_client import close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup hooks (warm caches, etc.) would go here.
    yield
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description="Persian RTL e-commerce — modular monolith, API-first.",
        lifespan=lifespan,
    )

    # `allow_origins` is an explicit list from configuration and must never
    # become ["*"]: combined with allow_credentials=True the browser would send
    # cookies and Authorization to any origin that asks. Starlette silently
    # ignores that combination, so the mistake fails quietly — assert instead.
    if "*" in settings.cors_origins:
        raise RuntimeError(
            "CORS allow_origins must not contain '*' while credentials are "
            "allowed; list the exact origins in CORS_ORIGINS."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)
    app.include_router(api_v1)

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        """Liveness only.

        The deployment environment used to be in this response. It is an
        unauthenticated endpoint, and naming the environment tells an attacker
        which target they have reached and which defaults might still be in
        place — free reconnaissance for no operational benefit, since whoever
        deploys already knows.
        """
        return {"status": "ok", "service": settings.app_name}

    return app


app = create_app()
