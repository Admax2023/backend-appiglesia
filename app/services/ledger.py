"""Shared money primitives: order row locks, the single place where an order's payment state is
recomputed, and amount validation for payments/proofs.

Lock ordering (to avoid deadlocks): order -> proof -> payment -> cash session.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.enums import CreditStatus, OrderStatus, PaymentRecordStatus, PaymentStatus
from app.errors import AppError, not_found
from app.models import BusinessSettings, DebtWriteOff, Order, Payment
from app.money import ZERO, q


def lock_order(db: Session, order_id: uuid.UUID) -> Order:
    order = db.scalar(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    if order is None:
        raise not_found("order_not_found", "Pedido no encontrado")
    return order


def recompute_order_payment_state(db: Session, order: Order) -> Order:
    """THE single place that derives amount_paid, amount_written_off, balance_due, payment_status."""
    db.flush()
    paid = q(
        db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.order_id == order.id, Payment.status == PaymentRecordStatus.completed
            )
        )
    )
    written_off = q(
        db.scalar(select(func.coalesce(func.sum(DebtWriteOff.amount), 0)).where(DebtWriteOff.order_id == order.id))
    )
    any_refunded = bool(
        db.scalar(
            select(
                exists().where(Payment.order_id == order.id, Payment.status == PaymentRecordStatus.refunded)
            )
        )
    )
    total = q(order.total)
    if paid + written_off > total:
        raise AppError(422, "amount_exceeds_balance", "El pago supera el saldo pendiente del pedido")

    order.amount_paid = paid
    order.amount_written_off = written_off
    order.balance_due = ZERO if order.status == OrderStatus.cancelled else q(total - paid - written_off)

    if total == ZERO and order.status != OrderStatus.cancelled:
        status = PaymentStatus.paid
    elif paid >= total and total > ZERO:
        status = PaymentStatus.paid
    elif paid + written_off >= total and written_off > ZERO:
        status = PaymentStatus.written_off
    elif paid > ZERO:
        status = PaymentStatus.partial
    elif any_refunded and order.status == OrderStatus.cancelled:
        # 'refunded' only describes a closed order whose money went back. An active order that still
        # owes its total is 'unpaid' (it must keep showing up in the unpaid filters).
        status = PaymentStatus.refunded
    else:
        status = PaymentStatus.unpaid
    order.payment_status = status
    if (
        order.credit_status == CreditStatus.pending_authorization
        and order.status != OrderStatus.cancelled
        and order.balance_due <= ZERO
    ):
        # Fiado requested but already fully paid: nothing left to authorize (otherwise the order
        # would be stuck: status changes need authorization, authorization needs a balance).
        order.credit_status = CreditStatus.not_needed
    db.flush()
    return order


def is_debt(order: Order) -> bool:
    return (
        order.status != OrderStatus.cancelled
        and order.balance_due > ZERO
        and (order.status == OrderStatus.delivered or order.credit_status == CreditStatus.authorized)
    )


def partial_allowed(order: Order, settings_row: BusinessSettings) -> bool:
    # Abonos against authorized credit (debts) are always partial by nature.
    return settings_row.allow_partial_payments or order.credit_status == CreditStatus.authorized or is_debt(order)


def validate_payment_amount(order: Order, amount: Decimal, settings_row: BusinessSettings) -> None:
    if order.status == OrderStatus.cancelled:
        raise AppError(409, "order_cancelled", "El pedido está cancelado")
    if order.balance_due <= ZERO:
        raise AppError(409, "order_already_paid", "El pedido no tiene saldo pendiente")
    if amount > order.balance_due:
        raise AppError(
            422,
            "amount_exceeds_balance",
            f"El importe ({q(amount)}) supera el saldo pendiente ({q(order.balance_due)})",
        )
    if amount < order.balance_due and not partial_allowed(order, settings_row):
        raise AppError(
            422,
            "partial_payments_not_allowed",
            f"No se permiten pagos parciales: el importe debe ser {q(order.balance_due)}",
        )
