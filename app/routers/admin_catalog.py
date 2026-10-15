"""Admin: products, settings, audit log, customers, debts."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.deps import AdminUser, DbSession, OptionalIdemKey, Pagination, client_ip, require_admin
from app.routers.common import ERR, DateFrom, DateTo, optional_range
from app.schemas.cash import AuditLogOut
from app.schemas.catalog import ProductCreate, ProductOut, ProductUpdate, SettingsOut, SettingsUpdate
from app.schemas.common import Page
from app.schemas.orders import (
    CustomerCreate,
    CustomerDebtSummaryOut,
    CustomerDetailOut,
    CustomerListItemOut,
    CustomerOut,
    CustomerUpdate,
    DebtOut,
    WriteOffIn,
    WriteOffOut,
    WriteOffResultOut,
)
from app.services import audit as audit_svc
from app.services import customers as customers_svc
from app.services import debts as debts_svc
from app.services import idempotency
from app.services import orders as orders_svc
from app.services import products as products_svc
from app.services import settings as settings_svc

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)], responses=ERR)


# --------------------------------------------------------------------------- products


@router.get("/products", response_model=Page[ProductOut], tags=["admin-products"])
def list_products(
    db: DbSession,
    pg: Pagination,
    include_inactive: bool = True,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[ProductOut]:
    rows, total = products_svc.list_products(
        db, offset=pg.offset, limit=pg.page_size, include_inactive=include_inactive, search=search
    )
    return Page[ProductOut].build([ProductOut.model_validate(p) for p in rows], total, pg.page, pg.page_size)


@router.post("/products", status_code=201, response_model=ProductOut, tags=["admin-products"])
def create_product(body: ProductCreate, request: Request, db: DbSession, user: AdminUser) -> ProductOut:
    p = products_svc.create(db, body, user, client_ip(request))
    db.commit()
    return ProductOut.model_validate(p)


@router.get("/products/{product_id}", response_model=ProductOut, tags=["admin-products"])
def get_product(product_id: uuid.UUID, db: DbSession) -> ProductOut:
    return ProductOut.model_validate(products_svc.get(db, product_id))


@router.patch("/products/{product_id}", response_model=ProductOut, tags=["admin-products"])
def update_product(
    product_id: uuid.UUID, body: ProductUpdate, request: Request, db: DbSession, user: AdminUser
) -> ProductOut:
    p = products_svc.update(db, product_id, body, user, client_ip(request))
    db.commit()
    return ProductOut.model_validate(p)


@router.post("/products/{product_id}/activate", response_model=ProductOut, tags=["admin-products"])
def activate_product(product_id: uuid.UUID, request: Request, db: DbSession, user: AdminUser) -> ProductOut:
    p = products_svc.set_active(db, product_id, True, user, client_ip(request))
    db.commit()
    return ProductOut.model_validate(p)


@router.post("/products/{product_id}/deactivate", response_model=ProductOut, tags=["admin-products"])
def deactivate_product(product_id: uuid.UUID, request: Request, db: DbSession, user: AdminUser) -> ProductOut:
    p = products_svc.set_active(db, product_id, False, user, client_ip(request))
    db.commit()
    return ProductOut.model_validate(p)


@router.delete("/products/{product_id}", status_code=204, tags=["admin-products"])
def delete_product(product_id: uuid.UUID, request: Request, db: DbSession, user: AdminUser) -> Response:
    products_svc.delete(db, product_id, user, client_ip(request))
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- settings


@router.get("/settings", response_model=SettingsOut, tags=["admin-settings"])
def get_settings_(db: DbSession) -> SettingsOut:
    return settings_svc.to_out(settings_svc.get_row(db))


@router.patch("/settings", response_model=SettingsOut, tags=["admin-settings"])
def update_settings(body: SettingsUpdate, request: Request, db: DbSession, user: AdminUser) -> SettingsOut:
    row = settings_svc.update(db, body, user, client_ip(request))
    db.commit()
    return settings_svc.to_out(row)


# --------------------------------------------------------------------------- audit


@router.get("/audit-logs", response_model=Page[AuditLogOut], tags=["admin-audit"])
def list_audit_logs(
    db: DbSession,
    pg: Pagination,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    action: Annotated[str | None, Query(max_length=60)] = None,
    entity_type: Annotated[str | None, Query(max_length=40)] = None,
    entity_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> Page[AuditLogOut]:
    start, end = optional_range(db, date_from, date_to)
    rows, total = audit_svc.list_logs(
        db, offset=pg.offset, limit=pg.page_size, action=action, entity_type=entity_type, entity_id=entity_id,
        user_id=user_id, start=start, end=end,
    )
    return Page[AuditLogOut].build([AuditLogOut.model_validate(r) for r in rows], total, pg.page, pg.page_size)


# --------------------------------------------------------------------------- customers


@router.get("/customers", response_model=Page[CustomerListItemOut], tags=["admin-customers"])
def list_customers(
    db: DbSession,
    pg: Pagination,
    search: Annotated[str | None, Query(max_length=100)] = None,
    has_debt: bool | None = None,
) -> Page[CustomerListItemOut]:
    items, total = customers_svc.list_customers(
        db, offset=pg.offset, limit=pg.page_size, search=search, has_debt=has_debt
    )
    return Page[CustomerListItemOut].build(items, total, pg.page, pg.page_size)


@router.post("/customers", status_code=201, response_model=CustomerOut, tags=["admin-customers"])
def create_customer(body: CustomerCreate, request: Request, db: DbSession, user: AdminUser) -> CustomerOut:
    c = customers_svc.create(db, body, user, client_ip(request))
    db.commit()
    return CustomerOut.model_validate(c)


@router.get("/customers/{customer_id}", response_model=CustomerDetailOut, tags=["admin-customers"])
def get_customer(customer_id: uuid.UUID, db: DbSession) -> CustomerDetailOut:
    return customers_svc.detail(db, customer_id)


@router.post("/customers/{customer_id}/verify", response_model=CustomerOut, tags=["admin-customers"])
def verify_customer(customer_id: uuid.UUID, request: Request, db: DbSession, user: AdminUser) -> CustomerOut:
    """Marca como verificado un cliente creado desde la web (409 phone_taken si ya hay uno verificado
    con ese teléfono: en ese caso vincula los pedidos a ese cliente)."""
    c = customers_svc.verify(db, customer_id, user, client_ip(request))
    db.commit()
    return CustomerOut.model_validate(c)


@router.patch("/customers/{customer_id}", response_model=CustomerOut, tags=["admin-customers"])
def update_customer(
    customer_id: uuid.UUID, body: CustomerUpdate, request: Request, db: DbSession, user: AdminUser
) -> CustomerOut:
    c = customers_svc.update(db, customer_id, body, user, client_ip(request))
    db.commit()
    return CustomerOut.model_validate(c)


# --------------------------------------------------------------------------- debts


@router.get("/debts", response_model=Page[DebtOut], tags=["admin-debts"])
def list_debts(
    db: DbSession,
    pg: Pagination,
    customer_id: uuid.UUID | None = None,
    min_age_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
) -> Page[DebtOut]:
    items, total = debts_svc.list_debt_rows(
        db, offset=pg.offset, limit=pg.page_size, customer_id=customer_id, min_age_days=min_age_days
    )
    return Page[DebtOut].build(items, total, pg.page, pg.page_size)


@router.get("/debts/by-customer", response_model=Page[CustomerDebtSummaryOut], tags=["admin-debts"])
def debts_by_customer(db: DbSession, pg: Pagination) -> Page[CustomerDebtSummaryOut]:
    items, total = debts_svc.by_customer(db, offset=pg.offset, limit=pg.page_size)
    return Page[CustomerDebtSummaryOut].build(items, total, pg.page, pg.page_size)


@router.post(
    "/orders/{order_id}/write-off", status_code=201, response_model=WriteOffResultOut, tags=["admin-debts"]
)
def write_off_debt(
    order_id: uuid.UUID, body: WriteOffIn, request: Request, db: DbSession, user: AdminUser, idem_key: OptionalIdemKey
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, WriteOffResultOut]:
        w, order = debts_svc.write_off(db, order_id, body.amount, body.reason, user, ip)
        return 201, WriteOffResultOut(writeoff=WriteOffOut.model_validate(w), order=orders_svc.summary(db, order))

    return idempotency.run(
        db, scope=f"admin:writeoff:{order_id}", key=idem_key, payload=body.model_dump(mode="json"), operation=op
    )
