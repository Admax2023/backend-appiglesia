from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.enums import CashMovementType, CashSessionStatus, DenominationKind, MovementDirection, PaymentMethod
from app.money import Money, MoneyIn, PositiveMoneyIn
from app.schemas.common import InModel, OptionalNote, ORMModel, Reason
from app.schemas.orders import CustomerDebtSummaryOut


class OpenCashIn(InModel):
    opening_amount: MoneyIn
    note: OptionalNote = None


class CashMovementIn(InModel):
    type: Literal["expense", "withdrawal", "deposit", "adjustment"]
    amount: PositiveMoneyIn
    direction: MovementDirection | None = Field(
        default=None,
        description="Obligatorio solo para 'adjustment' ('in' sobrante / 'out' faltante). "
        "Para los demás tipos se deduce (expense/withdrawal=out, deposit=in).",
    )
    reason: Reason


class CountLineIn(InModel):
    kind: DenominationKind
    denomination: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
    quantity: Annotated[int, Field(ge=0, le=10000)]


class CloseCashIn(InModel):
    counts: Annotated[list[CountLineIn], Field(max_length=40)]
    note: Annotated[str, Field(max_length=1000)] | None = Field(
        default=None, description="Obligatoria si el contado difiere del esperado."
    )


class CashMovementOut(ORMModel):
    id: uuid.UUID
    session_id: uuid.UUID
    type: CashMovementType
    direction: MovementDirection
    amount: Money
    reason: str | None
    payment_id: uuid.UUID | None
    refund_payment_id: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime


class CountLineOut(ORMModel):
    kind: DenominationKind
    denomination: Money
    quantity: int
    subtotal: Money


class CashSummaryOut(BaseModel):
    opening_amount: Money
    cash_sales: Money
    cash_refunds: Money
    expenses: Money
    withdrawals: Money
    deposits: Money
    adjustments_in: Money
    adjustments_out: Money
    total_in: Money
    total_out: Money
    expected_cash: Money
    cash_payments_count: int
    transfer_income: Money
    transfer_payments_count: int
    movements_count: int


class CashSessionOut(ORMModel):
    id: uuid.UUID
    status: CashSessionStatus
    opening_amount: Money
    opening_note: str | None
    opened_at: datetime
    opened_by_name: str | None
    closed_at: datetime | None
    closed_by_name: str | None
    expected_cash: Money | None
    counted_cash: Money | None
    difference: Money | None
    closing_note: str | None


class CashSessionDetailOut(CashSessionOut):
    summary: CashSummaryOut
    count_lines: list[CountLineOut]


class DenominationsOut(BaseModel):
    currency: str = "COP"
    bills: list[Money]
    coins: list[Money]


# --------------------------------------------------------------------------- reports


class DaySalesOut(BaseModel):
    date: date
    orders_count: int
    total_sold: Money


class SalesReportOut(BaseModel):
    date_from: date
    date_to: date
    timezone: str
    orders_count: int
    cancelled_orders_count: int
    total_sold: Money
    collected: Money
    written_off: Money
    pending: Money
    payments_received: Money
    refunds: Money
    net_received: Money
    by_day: list[DaySalesOut]


class ProductSalesOut(BaseModel):
    product_id: uuid.UUID
    product_name: str
    quantity: int
    total: Money


class ProductsReportOut(BaseModel):
    date_from: date
    date_to: date
    timezone: str
    items: list[ProductSalesOut]


class MethodPaymentsOut(BaseModel):
    method: PaymentMethod
    payments_count: int
    amount: Money
    refunded_amount: Money


class MethodOrdersOut(BaseModel):
    payment_method: PaymentMethod
    orders_count: int
    total: Money


class PaymentMethodsReportOut(BaseModel):
    date_from: date
    date_to: date
    timezone: str
    payments: list[MethodPaymentsOut]
    orders: list[MethodOrdersOut]


class CashClosingsReportOut(BaseModel):
    date_from: date
    date_to: date
    timezone: str
    sessions_count: int
    total_expected: Money
    total_counted: Money
    total_difference: Money
    total_surplus: Money
    total_shortage: Money
    sessions: list[CashSessionOut]


class DebtsReportOut(BaseModel):
    total_outstanding: Money
    debt_orders_count: int
    customers_count: int
    by_customer: list[CustomerDebtSummaryOut]


class AuditLogOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    user_name: str | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    details: dict[str, Any]
    ip_address: str | None
    created_at: datetime
