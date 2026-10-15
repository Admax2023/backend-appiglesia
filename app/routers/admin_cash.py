"""Admin: cash sessions and reports."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.deps import AdminUser, DbSession, OptionalIdemKey, Pagination, client_ip, require_admin
from app.enums import CashSessionStatus
from app.errors import AppError
from app.routers.common import ERR, DateFrom, DateTo, optional_range, resolve_dates
from app.schemas.cash import (
    CashClosingsReportOut,
    CashMovementIn,
    CashMovementOut,
    CashSessionDetailOut,
    CashSessionOut,
    CloseCashIn,
    DebtsReportOut,
    DenominationsOut,
    OpenCashIn,
    PaymentMethodsReportOut,
    ProductsReportOut,
    SalesReportOut,
)
from app.schemas.common import Page
from app.services import cash as cash_svc
from app.services import idempotency
from app.services import reports as reports_svc

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)], responses=ERR)


@router.get("/cash/denominations", response_model=DenominationsOut, tags=["admin-cash"])
def denominations() -> DenominationsOut:
    s = get_settings()
    return DenominationsOut(bills=s.bill_denominations, coins=s.coin_denominations)


@router.post("/cash/open", status_code=201, response_model=CashSessionDetailOut, tags=["admin-cash"])
def open_cash(
    body: OpenCashIn, request: Request, db: DbSession, user: AdminUser, idem_key: OptionalIdemKey
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, CashSessionDetailOut]:
        s = cash_svc.open_session(db, body.opening_amount, body.note, user, ip)
        return 201, cash_svc.detail(db, s)

    return idempotency.run(db, scope="admin:cash_open", key=idem_key, payload=body.model_dump(mode="json"), operation=op)


@router.get("/cash/current", response_model=CashSessionDetailOut, tags=["admin-cash"])
def current_cash(db: DbSession) -> CashSessionDetailOut:
    s = cash_svc.get_open_session(db)
    if s is None:
        raise AppError(404, "no_open_cash_session", "No hay una caja abierta")
    return cash_svc.detail(db, s)


@router.post("/cash/movements", status_code=201, response_model=CashMovementOut, tags=["admin-cash"])
def create_movement(
    body: CashMovementIn, request: Request, db: DbSession, user: AdminUser, idem_key: OptionalIdemKey
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, CashMovementOut]:
        m = cash_svc.add_manual_movement(db, body, user, ip)
        return 201, CashMovementOut.model_validate(m)

    return idempotency.run(
        db, scope="admin:cash_movement", key=idem_key, payload=body.model_dump(mode="json"), operation=op
    )


@router.get("/cash/sessions", response_model=Page[CashSessionOut], tags=["admin-cash"])
def list_sessions(
    db: DbSession, pg: Pagination, status: CashSessionStatus | None = None,
    date_from: DateFrom = None, date_to: DateTo = None,
) -> Page[CashSessionOut]:
    start, end = optional_range(db, date_from, date_to)
    rows, total = cash_svc.list_sessions(db, offset=pg.offset, limit=pg.page_size, status=status, start=start, end=end)
    return Page[CashSessionOut].build([CashSessionOut.model_validate(s) for s in rows], total, pg.page, pg.page_size)


@router.get("/cash/sessions/{session_id}", response_model=CashSessionDetailOut, tags=["admin-cash"])
def get_session(session_id: uuid.UUID, db: DbSession) -> CashSessionDetailOut:
    return cash_svc.detail(db, cash_svc.get_session(db, session_id))


@router.get("/cash/sessions/{session_id}/movements", response_model=Page[CashMovementOut], tags=["admin-cash"])
def list_movements(session_id: uuid.UUID, db: DbSession, pg: Pagination) -> Page[CashMovementOut]:
    rows, total = cash_svc.list_movements(db, session_id, offset=pg.offset, limit=pg.page_size)
    return Page[CashMovementOut].build([CashMovementOut.model_validate(m) for m in rows], total, pg.page, pg.page_size)


@router.post("/cash/sessions/{session_id}/close", response_model=CashSessionDetailOut, tags=["admin-cash"])
def close_cash(
    session_id: uuid.UUID, body: CloseCashIn, request: Request, db: DbSession, user: AdminUser,
    idem_key: OptionalIdemKey,
) -> JSONResponse:
    ip = client_ip(request)

    def op() -> tuple[int, CashSessionDetailOut]:
        s = cash_svc.close_session(db, session_id, body, user, ip)
        return 200, cash_svc.detail(db, s)

    return idempotency.run(
        db, scope=f"admin:cash_close:{session_id}", key=idem_key, payload=body.model_dump(mode="json"), operation=op
    )


# --------------------------------------------------------------------------- reports


@router.get("/reports/sales", response_model=SalesReportOut, tags=["admin-reports"])
def report_sales(db: DbSession, date_from: DateFrom = None, date_to: DateTo = None) -> SalesReportOut:
    d1, d2, tz = resolve_dates(db, date_from, date_to)
    return reports_svc.sales(db, d1, d2, tz)


@router.get("/reports/products", response_model=ProductsReportOut, tags=["admin-reports"])
def report_products(db: DbSession, date_from: DateFrom = None, date_to: DateTo = None) -> ProductsReportOut:
    d1, d2, tz = resolve_dates(db, date_from, date_to)
    return reports_svc.products(db, d1, d2, tz)


@router.get("/reports/payment-methods", response_model=PaymentMethodsReportOut, tags=["admin-reports"])
def report_methods(db: DbSession, date_from: DateFrom = None, date_to: DateTo = None) -> PaymentMethodsReportOut:
    d1, d2, tz = resolve_dates(db, date_from, date_to)
    return reports_svc.payment_methods(db, d1, d2, tz)


@router.get("/reports/cash-closings", response_model=CashClosingsReportOut, tags=["admin-reports"])
def report_cash(db: DbSession, date_from: DateFrom = None, date_to: DateTo = None) -> CashClosingsReportOut:
    d1, d2, tz = resolve_dates(db, date_from, date_to)
    return reports_svc.cash_closings(db, d1, d2, tz)


@router.get("/reports/debts", response_model=DebtsReportOut, tags=["admin-reports"])
def report_debts(db: DbSession) -> DebtsReportOut:
    return reports_svc.debts_report(db)
