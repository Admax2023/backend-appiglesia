"""Health, auth and public (customer) endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.config import get_settings
from app.deps import CurrentUser, DbSession, RequiredIdemKey, client_ip
from app.errors import AppError
from app.ratelimit import hit
from app.routers.common import ERR
from app.schemas.auth import ChangePasswordIn, LoginIn, LogoutIn, RefreshIn, TokenOut, UserOut
from app.schemas.catalog import PublicMenu, PublicProduct, PublicSettings
from app.schemas.orders import (
    OrderCreate,
    OrderQuoteIn,
    OrderQuoteOut,
    PriceChangedErrorOut,
    PublicOrderCreatedOut,
    PublicOrderOut,
    PublicProofOut,
)
from app.services import auth as auth_svc
from app.services import idempotency
from app.services import orders as orders_svc
from app.services import payments as payments_svc
from app.services import products as products_svc
from app.services import settings as settings_svc
from app.services import storage

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- health


@router.get("/health", tags=["health"])
def health(db: DbSession) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok", "version": __version__}


# --------------------------------------------------------------------------- auth


@router.post("/auth/login", response_model=TokenOut, tags=["auth"], responses=ERR)
def login(body: LoginIn, request: Request, db: DbSession) -> TokenOut:
    ip = client_ip(request)
    hit("login", ip)  # per IP only: changing the username must not open a fresh bucket
    hit("login_user", body.username.lower())
    out = auth_svc.login(db, body.username, body.password, request.headers.get("user-agent"), ip)
    db.commit()
    return out


@router.post("/auth/refresh", response_model=TokenOut, tags=["auth"], responses=ERR)
def refresh(body: RefreshIn, request: Request, db: DbSession) -> TokenOut:
    ip = client_ip(request)
    hit("refresh", ip)
    out = auth_svc.refresh(db, body.refresh_token, request.headers.get("user-agent"), ip)
    db.commit()
    return out


@router.post("/auth/logout", status_code=204, tags=["auth"], responses=ERR)
def logout(body: LogoutIn, db: DbSession) -> Response:
    auth_svc.logout(db, body.refresh_token)
    db.commit()
    return Response(status_code=204)


@router.get("/auth/me", response_model=UserOut, tags=["auth"], responses=ERR)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/auth/change-password", status_code=204, tags=["auth"], responses=ERR)
def change_password(body: ChangePasswordIn, request: Request, user: CurrentUser, db: DbSession) -> Response:
    auth_svc.change_password(db, user, body.current_password, body.new_password, client_ip(request))
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- public


@router.get("/public/menu", response_model=PublicMenu, tags=["public"])
def public_menu(request: Request, db: DbSession) -> PublicMenu:
    hit("public_read", client_ip(request))
    srow = settings_svc.get_row(db)
    return PublicMenu(
        business_name=srow.business_name,
        accepting_orders=srow.accepting_orders,
        products=[PublicProduct.model_validate(p) for p in products_svc.public_menu(db)],
    )


@router.get("/public/settings", response_model=PublicSettings, tags=["public"])
def public_settings(request: Request, db: DbSession) -> PublicSettings:
    hit("public_read", client_ip(request))
    return settings_svc.to_public(settings_svc.get_row(db))


@router.post(
    "/public/orders/quote",
    response_model=OrderQuoteOut,
    tags=["public"],
    responses={**ERR, 429: {"description": "Rate limit"}},
)
def quote_public_order(body: OrderQuoteIn, request: Request, db: DbSession) -> OrderQuoteOut:
    """Precio calculado por el servidor para un carrito. No guarda nada (ni cliente, ni pedido, ni
    Idempotency-Key)."""
    hit("order_quote", client_ip(request))
    out = orders_svc.quote(db, body)
    db.rollback()  # read-only: make sure nothing is ever persisted
    return out


@router.post(
    "/public/orders",
    status_code=201,
    response_model=PublicOrderCreatedOut,
    tags=["public"],
    responses={
        **ERR,
        409: {
            "model": PriceChangedErrorOut,
            "description": "Conflicto (`orders_closed`, `duplicate_order`, `idempotency_key_conflict`, "
            "`price_changed`). Solo `price_changed` incluye `quote`.",
        },
        429: {"description": "Rate limit"},
    },
)
def create_public_order(body: OrderCreate, request: Request, db: DbSession, idem_key: RequiredIdemKey) -> JSONResponse:
    ip = client_ip(request)
    hit("create_order", ip)

    def op() -> tuple[int, PublicOrderCreatedOut]:
        order, token = orders_svc.create_order(db, body, actor=None, ip=ip)
        return 201, PublicOrderCreatedOut(tracking_token=token, order=orders_svc.public_view(db, order))

    return idempotency.run(
        db, scope="public:create_order", key=idem_key, payload=body.model_dump(mode="json"), operation=op,
        secret_fields=("tracking_token",),
    )


@router.get("/public/orders/{tracking_token}", response_model=PublicOrderOut, tags=["public"], responses=ERR)
def public_order_status(tracking_token: str, request: Request, db: DbSession) -> PublicOrderOut:
    hit("order_status", client_ip(request))
    order = orders_svc.get_by_token(db, tracking_token)
    return orders_svc.public_view(db, order)


@router.post(
    "/public/orders/{tracking_token}/proofs",
    status_code=201,
    response_model=PublicProofOut,
    tags=["public"],
    responses={**ERR, 413: {"description": "Archivo demasiado grande"}, 415: {"description": "Tipo no permitido"}},
)
def upload_proof(
    tracking_token: str,
    request: Request,
    db: DbSession,
    file: Annotated[UploadFile, File(description="Imagen JPEG, PNG o WEBP (máx. 5 MB)")],
    declared_amount: Annotated[Decimal, Form(gt=0, max_digits=12, decimal_places=2, description="Importe transferido")],
) -> PublicProofOut:
    hit("upload_proof", client_ip(request))
    limit = get_settings().max_upload_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise AppError(413, "file_too_large", f"El archivo supera el máximo de {limit} bytes")
    proof, _order, key = payments_svc.upload_proof(db, tracking_token, data, file.filename, declared_amount)
    try:
        db.commit()
    except Exception:
        db.rollback()
        storage.delete_quietly(key)
        raise
    return PublicProofOut.model_validate(proof)
