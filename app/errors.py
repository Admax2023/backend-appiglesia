"""Consistent error format: {"detail": str, "code": str} (+ "errors" list for validation errors)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("app.errors")


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(detail)
        self.extra = extra  # additional top-level fields in the error body (e.g. "quote" for price_changed)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = headers


def not_found(code: str = "not_found", detail: str = "Recurso no encontrado") -> AppError:
    return AppError(404, code, detail)


def conflict(code: str, detail: str) -> AppError:
    return AppError(409, code, detail)


def unprocessable(code: str, detail: str) -> AppError:
    return AppError(422, code, detail)


_HTTP_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def _clean_errors(errors: list[Any]) -> list[dict[str, Any]]:
    out = []
    for e in errors:
        out.append(
            {
                "loc": [str(x) for x in e.get("loc", [])],
                "msg": str(e.get("msg", "")),
                "type": str(e.get("type", "")),
            }
        )
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={**(exc.extra or {}), "detail": exc.detail, "code": exc.code},
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "error")
        detail = exc.detail if isinstance(exc.detail, str) else "Error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail, "code": code},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Datos inválidos",
                "code": "validation_error",
                "errors": _clean_errors(list(exc.errors())),
            },
        )

    @app.exception_handler(DataError)
    async def _data_error(_: Request, exc: DataError) -> JSONResponse:
        # NUMERIC(12,2) overflow (SQLSTATE 22003) is a client input problem, not a server error.
        if getattr(exc.orig, "sqlstate", None) == "22003":
            return JSONResponse(
                status_code=422,
                content={"detail": "Un importe supera el máximo permitido", "code": "amount_too_large"},
            )
        log.exception("Unhandled data error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Error interno", "code": "internal_error"})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Error interno", "code": "internal_error"})
