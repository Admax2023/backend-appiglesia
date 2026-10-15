from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    admin = "admin"


class OrderStatus(StrEnum):
    pending_confirmation = "pending_confirmation"
    confirmed = "confirmed"
    preparing = "preparing"
    ready = "ready"
    delivered = "delivered"
    cancelled = "cancelled"


class PaymentStatus(StrEnum):
    """Per-order payment state, always derived by services.payments.recompute_order_payment_state."""

    unpaid = "unpaid"
    partial = "partial"
    paid = "paid"
    refunded = "refunded"
    written_off = "written_off"


class CreditStatus(StrEnum):
    none = "none"
    pending_authorization = "pending_authorization"
    authorized = "authorized"
    rejected = "rejected"
    # Credit was requested but the order got fully paid before an admin decided: nothing to authorize.
    not_needed = "not_needed"


class PaymentMethod(StrEnum):
    cash = "cash"
    transfer = "transfer"
    credit = "credit"


class PaymentRecordStatus(StrEnum):
    completed = "completed"
    refunded = "refunded"


class ProofStatus(StrEnum):
    pending_verification = "pending_verification"
    approved = "approved"
    rejected = "rejected"
    refunded = "refunded"


class CashSessionStatus(StrEnum):
    open = "open"
    closed = "closed"


class CashMovementType(StrEnum):
    sale_cash = "sale_cash"
    refund_cash = "refund_cash"
    expense = "expense"
    withdrawal = "withdrawal"
    deposit = "deposit"
    adjustment = "adjustment"


class MovementDirection(StrEnum):
    in_ = "in"
    out = "out"


class DenominationKind(StrEnum):
    bill = "bill"
    coin = "coin"


# Order status transition graph (cancellation handled by the dedicated cancel action).
ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.pending_confirmation: {OrderStatus.confirmed, OrderStatus.cancelled},
    OrderStatus.confirmed: {
        OrderStatus.preparing,
        OrderStatus.ready,
        OrderStatus.delivered,
        OrderStatus.cancelled,
    },
    OrderStatus.preparing: {OrderStatus.ready, OrderStatus.delivered, OrderStatus.cancelled},
    OrderStatus.ready: {OrderStatus.delivered, OrderStatus.cancelled},
    OrderStatus.delivered: set(),
    OrderStatus.cancelled: set(),
}
