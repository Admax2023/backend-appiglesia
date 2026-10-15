from __future__ import annotations

from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.enums import CashSessionStatus, OrderStatus, PaymentMethod, PaymentRecordStatus
from app.models import CashSession, Order, OrderItem, Payment, Product
from app.money import ZERO, q
from app.schemas.cash import (
    CashClosingsReportOut,
    CashSessionOut,
    DaySalesOut,
    DebtsReportOut,
    MethodOrdersOut,
    MethodPaymentsOut,
    PaymentMethodsReportOut,
    ProductSalesOut,
    ProductsReportOut,
    SalesReportOut,
)
from app.services import debts
from app.timeutil import day_range_utc, get_zone


def _range(date_from: date, date_to: date, tz_name: str):
    tz = get_zone(tz_name)
    return day_range_utc(date_from, date_to, tz)


def sales(db: Session, date_from: date, date_to: date, tz_name: str) -> SalesReportOut:
    start, end = _range(date_from, date_to, tz_name)
    in_range = [Order.created_at >= start, Order.created_at < end]
    active = [*in_range, Order.status != OrderStatus.cancelled]
    row = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.total), 0),
            func.coalesce(func.sum(Order.amount_paid), 0),
            func.coalesce(func.sum(Order.amount_written_off), 0),
            func.coalesce(func.sum(Order.balance_due), 0),
        ).where(*active)
    ).one()
    cancelled = db.scalar(
        select(func.count(Order.id)).where(*in_range, Order.status == OrderStatus.cancelled)
    ) or 0
    received = q(db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.created_at >= start, Payment.created_at < end)
    ))
    refunds = q(db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentRecordStatus.refunded, Payment.refunded_at >= start, Payment.refunded_at < end
        )
    ))
    local_day = func.date(func.timezone(tz_name, Order.created_at))
    days = db.execute(
        select(local_day.label("d"), func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
        .where(*active)
        .group_by("d")
        .order_by("d")
    ).all()
    return SalesReportOut(
        date_from=date_from, date_to=date_to, timezone=tz_name,
        orders_count=int(row[0]), cancelled_orders_count=int(cancelled),
        total_sold=q(row[1]), collected=q(row[2]), written_off=q(row[3]), pending=q(row[4]),
        payments_received=received, refunds=refunds, net_received=q(received - refunds),
        by_day=[DaySalesOut(date=d, orders_count=int(n), total_sold=q(t)) for d, n, t in days],
    )


def products(db: Session, date_from: date, date_to: date, tz_name: str) -> ProductsReportOut:
    start, end = _range(date_from, date_to, tz_name)
    rows = db.execute(
        select(
            OrderItem.product_id,
            func.coalesce(func.max(Product.name), func.max(OrderItem.product_name)),
            func.sum(OrderItem.quantity),
            func.sum(OrderItem.line_total),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .outerjoin(Product, Product.id == OrderItem.product_id)
        .where(Order.created_at >= start, Order.created_at < end, Order.status != OrderStatus.cancelled)
        .group_by(OrderItem.product_id)
        .order_by(func.sum(OrderItem.line_total).desc())
    ).all()
    return ProductsReportOut(
        date_from=date_from, date_to=date_to, timezone=tz_name,
        items=[ProductSalesOut(product_id=pid, product_name=name, quantity=int(qty), total=q(t))
               for pid, name, qty, t in rows],
    )


def payment_methods(db: Session, date_from: date, date_to: date, tz_name: str) -> PaymentMethodsReportOut:
    start, end = _range(date_from, date_to, tz_name)
    prow = db.execute(
        select(
            Payment.method,
            func.count(Payment.id),
            func.coalesce(func.sum(case((Payment.status == PaymentRecordStatus.completed, Payment.amount), else_=0)), 0),
            func.coalesce(func.sum(case((Payment.status == PaymentRecordStatus.refunded, Payment.amount), else_=0)), 0),
        )
        .where(Payment.created_at >= start, Payment.created_at < end)
        .group_by(Payment.method)
    ).all()
    pmap = {m: (int(c), q(a), q(r)) for m, c, a, r in prow}
    orow = db.execute(
        select(Order.payment_method, func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
        .where(Order.created_at >= start, Order.created_at < end, Order.status != OrderStatus.cancelled)
        .group_by(Order.payment_method)
    ).all()
    omap = {m: (int(c), q(t)) for m, c, t in orow}
    return PaymentMethodsReportOut(
        date_from=date_from, date_to=date_to, timezone=tz_name,
        payments=[
            MethodPaymentsOut(method=m, payments_count=pmap.get(m, (0, ZERO, ZERO))[0],
                              amount=pmap.get(m, (0, ZERO, ZERO))[1], refunded_amount=pmap.get(m, (0, ZERO, ZERO))[2])
            for m in (PaymentMethod.cash, PaymentMethod.transfer)
        ],
        orders=[
            MethodOrdersOut(payment_method=m, orders_count=omap.get(m, (0, ZERO))[0], total=omap.get(m, (0, ZERO))[1])
            for m in PaymentMethod
        ],
    )


def cash_closings(db: Session, date_from: date, date_to: date, tz_name: str) -> CashClosingsReportOut:
    start, end = _range(date_from, date_to, tz_name)
    sessions = db.scalars(
        select(CashSession)
        .where(CashSession.status == CashSessionStatus.closed, CashSession.closed_at >= start,
               CashSession.closed_at < end)
        .order_by(CashSession.closed_at.desc())
    ).all()
    exp = sum((s.expected_cash or ZERO for s in sessions), ZERO)
    cnt = sum((s.counted_cash or ZERO for s in sessions), ZERO)
    diffs = [s.difference or ZERO for s in sessions]
    return CashClosingsReportOut(
        date_from=date_from, date_to=date_to, timezone=tz_name, sessions_count=len(sessions),
        total_expected=q(exp), total_counted=q(cnt), total_difference=q(sum(diffs, ZERO)),
        total_surplus=q(sum((d for d in diffs if d > 0), ZERO)),
        total_shortage=q(-sum((d for d in diffs if d < 0), ZERO)),
        sessions=[CashSessionOut.model_validate(s) for s in sessions],
    )


def debts_report(db: Session) -> DebtsReportOut:
    total, n_orders, n_customers = debts.totals(db)
    by_c, _ = debts.by_customer(db)
    return DebtsReportOut(total_outstanding=total, debt_orders_count=n_orders, customers_count=n_customers,
                          by_customer=by_c)
