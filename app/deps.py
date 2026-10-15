from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import UserRole
from app.errors import AppError
from app.models import User
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False, description="JWT access token from /api/auth/login")

DbSession = Annotated[Session, Depends(get_db)]


def _unauthorized(detail: str = "No autenticado", code: str = "not_authenticated") -> AppError:
    return AppError(401, code, detail, headers={"WWW-Authenticate": "Bearer"})


def get_current_user(
    db: DbSession,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized()
    try:
        payload = decode_access_token(creds.credentials)
        user_id = uuid.UUID(str(payload["sub"]))
    except jwt.ExpiredSignatureError:
        raise _unauthorized("El token de acceso expiró", "token_expired")
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise _unauthorized("Token inválido", "invalid_token")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("Usuario inválido o inactivo", "invalid_token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.admin:
        raise AppError(403, "forbidden", "No tienes permiso para esta operación")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def client_ip(request: Request) -> str:
    """Client IP used for rate limiting and audit.

    Without TRUST_PROXY_HEADERS the TCP peer is used. Behind TRUSTED_PROXY_HOPS reverse proxies, each
    proxy APPENDS the address it saw to X-Forwarded-For, so only the rightmost N entries are trustworthy;
    the client is the N-th entry from the right. Everything to the left of it was sent by the client
    and is ignored (taking the leftmost entry would let anyone spoof their IP and bypass rate limits).
    """
    peer = (request.client.host if request.client else "unknown")[:64]
    s = get_settings()
    if not s.trust_proxy_headers:
        return peer
    # Several X-Forwarded-For headers are equivalent to one comma-joined list (RFC 7230 §3.2.2).
    hops = [h.strip() for v in request.headers.getlist("x-forwarded-for") for h in v.split(",") if h.strip()]
    if not hops:
        return peer
    n = s.trusted_proxy_hops
    # Fewer entries than trusted proxies: every entry was written by a trusted proxy, the first is the client.
    return (hops[-n] if len(hops) >= n else hops[0])[:64]


@dataclass
class PageParams:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: Annotated[int, Query(ge=1, le=100000, description="Página (1-based)")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Elementos por página (máx. 100)")] = 20,
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


Pagination = Annotated[PageParams, Depends(page_params)]

_IDEM_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,255}$")


def _validate_idem(key: str | None) -> str | None:
    if key is None:
        return None
    key = key.strip()
    if not _IDEM_RE.match(key):
        raise AppError(
            400,
            "invalid_idempotency_key",
            "Idempotency-Key debe tener entre 8 y 255 caracteres [A-Za-z0-9_-:.] (se recomienda un UUID v4)",
        )
    return key


def optional_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="Clave única por operación (UUID v4 recomendado). Opcional."),
    ] = None,
) -> str | None:
    return _validate_idem(idempotency_key)


def required_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="OBLIGATORIO. Clave única por operación (UUID v4)."),
    ] = None,
) -> str:
    key = _validate_idem(idempotency_key)
    if key is None:
        raise AppError(400, "idempotency_key_required", "Falta el encabezado Idempotency-Key")
    return key


OptionalIdemKey = Annotated[str | None, Depends(optional_idempotency_key)]
RequiredIdemKey = Annotated[str, Depends(required_idempotency_key)]
