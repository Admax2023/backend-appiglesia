"""Pure-ASGI middlewares: request body size limit and security headers."""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings


class _BodyTooLarge(Exception):
    pass


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    raw = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": raw})


class BodySizeLimitMiddleware:
    """Rejects bodies larger than the configured limit (413) using Content-Length and by
    counting streamed bytes (chunked uploads). Multipart proof uploads get a larger limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        s = get_settings()
        self.json_limit = s.max_json_body_bytes
        self.upload_limit = s.max_upload_bytes + 64 * 1024  # multipart overhead

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        limit = self.upload_limit if path.endswith("/proofs") else self.json_limit
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await _send_json(
                            send, 413, {"detail": "El cuerpo de la solicitud es demasiado grande", "code": "payload_too_large"}
                        )
                        return
                except ValueError:
                    await _send_json(send, 400, {"detail": "Content-Length inválido", "code": "bad_request"})
                    return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _send_json(
                    send, 413, {"detail": "El cuerpo de la solicitud es demasiado grande", "code": "payload_too_large"}
                )


SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-site"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.hsts = get_settings().is_production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        is_docs = path.startswith("/api/docs") or path.startswith("/api/redoc")
        no_store = path.startswith("/api/admin") or path.startswith("/api/auth") or path.startswith("/api/public/orders")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS:
                    if k not in existing:
                        headers.append((k, v))
                if not is_docs and b"content-security-policy" not in existing:
                    headers.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
                if no_store and b"cache-control" not in existing:
                    headers.append((b"cache-control", b"no-store"))
                if self.hsts:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
