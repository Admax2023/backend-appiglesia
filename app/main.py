from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, logfilters
from app.config import get_settings
from app.errors import install_error_handlers
from app.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.routers import admin_cash, admin_catalog, admin_orders, auth_public

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logfilters.install()  # keep tracking tokens out of the access log


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from app.services.idempotency import purger

    logfilters.install()  # uvicorn may (re)configure its loggers after import
    purger.start()
    try:
        yield
    finally:
        purger.stop()


def create_app() -> FastAPI:
    s = get_settings()
    docs = not s.is_production
    app = FastAPI(
        title="Iglesia — API de pedidos, pagos y caja",
        version=__version__,
        description="API REST del sistema de venta de comida frita. Ver docs/API.md.",
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(auth_public.router)
    app.include_router(admin_orders.router)
    app.include_router(admin_catalog.router)
    app.include_router(admin_cash.router)

    # Order matters: the last added middleware is the outermost.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["Idempotent-Replayed", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


app = create_app()
