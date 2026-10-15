"""Debts = outstanding balance of orders that were delivered or have authorized credit (fiado)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.enums import CreditStatus, OrderStatus, PaymentRecordStatus
from app.errors import AppError
from app.models import DebtWriteOff, Order, Payment, User
from app.money import ZERO, q
from app.schemas.orders import CustomerDebtSummaryOut, DebtOut
from app.services import audit, ledger


def debt_condition():
    return and_(
        Order.status != OrderStatus.cancelled,
        Order.balance_due > 0,
        or_(Order.status == OrderStatus.delivered, Order.credit_status == CreditStatus.authorized),
    )


def _age_days(created: datetime) -> int:
    return max(0, (datetime.now(timezone.utc) - created).days)


def list_debt_rows(
    db: Session, *, offset: int, limit: int, customer_id: uuid.UUID | None = None, min_age_days: int | None = None
) -> tuple[list[DebtOut], int]:
    conds = [debt_condition()]
    if customer_id:
        conds.append(Order.customer_id == customer_id)
    if min_age_days:
        conds.append(Order.created_at <= func.now() - func.make_interval(0, 0, 0, min_age_days))
    last_pay = (
        select(func.max(Payment.created_at))
        .where(Payment.order_id == Order.id, Payment.status == PaymentRecordStatus.completed)
        .correlate(Order)
        .scalar_subquery()
    )
    total = db.scalar(select(func.count()).select_from(Order).where(*conds)) or 0
    rows = db.execute(
        select(Order, last_pay).where(*conds).order_by(Order.created_at.asc(), Order.id).offset(offset).limit(limit)
    ).all()
    out = [
        DebtOut(
            order_id=o.id, order_code=o.code, customer_id=o.customer_id, customer_name=o.customer_name,
            customer_phone=o.customer_phone, order_status=o.status, credit_status=o.credit_status, total=o.total,
            amount_paid=o.amount_paid, amount_written_off=o.amount_written_off, balance_due=o.balance_due,
            created_at=o.created_at, age_days=_age_days(o.created_at), last_payment_at=lp,
        )
        for o, lp in rows
    ]
    return out, total


def by_customer(db: Session, *, offset: int = 0, limit: int = 1000) -> tuple[list[CustomerDebtSummaryOut], int]:
    from app.models import Customer

    base = (
        select(
            Order.customer_id,
            func.count(Order.id).label("n"),
            func.sum(Order.balance_due).label("due"),
            func.min(Order.created_at).label("oldest"),
        )
        .where(debt_condition())
        .group_by(Order.customer_id)
        .subquery()
    )
    total = db.scalar(select(func.count()).select_from(base)) or 0
    rows = db.execute(
        select(Customer, base.c.n, base.c.due, base.c.oldest)
        .join(base, base.c.customer_id == Customer.id)
        .order_by(base.c.due.desc(), Customer.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        CustomerDebtSummaryOut(
            customer_id=c.id, customer_name=c.name, customer_phone=c.phone, debt_orders_count=int(n),
            total_due=q(due), oldest_debt_at=oldest, max_age_days=_age_days(oldest),
        )
        for c, n, due, oldest in rows
    ], total


def totals(db: Session) -> tuple[Decimal, int, int]:
    row = db.execute(
        select(func.coalesce(func.sum(Order.balance_due), 0), func.count(Order.id),
               func.count(func.distinct(Order.customer_id))).where(debt_condition())
    ).one()
    return q(row[0]), int(row[1]), int(row[2])


def write_off(
    db: Session, order_id: uuid.UUID, amount: Decimal | None, reason: str, actor: User, ip: str | None
) -> tuple[DebtWriteOff, Order]:
    order = ledger.lock_order(db, order_id)
    if not ledger.is_debt(order):
        raise AppError(409, "not_a_debt",
                       "Solo se pueden condonar saldos de pedidos entregados o con fiado autorizado")
    value = q(amount) if amount is not None else q(order.balance_due)
    if value <= ZERO or value > order.balance_due:
        raise AppError(422, "amount_exceeds_balance", f"El importe debe estar entre 0.01 y {q(order.balance_due)}")
    w = DebtWriteOff(order_id=order.id, amount=value, reason=reason, created_by_id=actor.id)
    db.add(w)
    db.flush()
    ledger.recompute_order_payment_state(db, order)
    audit.record(db, user=actor, action="debt_written_off", entity_type="order", entity_id=order.id,
                 details={"order_code": order.code, "amount": value, "reason": reason,
                          "balance_due": order.balance_due}, ip=ip)
    return w, order
