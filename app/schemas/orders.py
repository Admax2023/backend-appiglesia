"""Orders, payments, proofs, debts and customers schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.config import get_settings
from app.enums import CreditStatus, OrderStatus, PaymentMethod, PaymentRecordStatus, PaymentStatus, ProofStatus
from app.money import Money, MoneyIn, PositiveMoneyIn
from app.schemas.common import ErrorOut, InModel, OptionalNote, ORMModel, Reason

_s = get_settings()
_PHONE_RE = re.compile(r"^[0-9+()\-. ]{7,30}$")

CustomerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]


def normalize_phone(phone: str | None) -> str | None:
    if phone is None:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 12 and digits.startswith("57"):
        digits = digits[2:]
    return digits or None


def _validate_phone(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if v == "":
        return None
    if not _PHONE_RE.match(v):
        raise ValueError("teléfono inválido")
    n = normalize_phone(v)
    if n is None or not (7 <= len(n) <= 15):
        raise ValueError("teléfono inválido (7 a 15 dígitos)")
    return v


# --------------------------------------------------------------------------- input


class OrderItemIn(BaseModel):
    # extra="ignore": any price/total sent by a client is silently ignored; prices come from the DB.
    model_config = ConfigDict(extra="ignore")
    product_id: uuid.UUID
    quantity: Annotated[int, Field(ge=1, le=_s.max_quantity_per_line)]


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    customer_name: CustomerName
    customer_phone: Annotated[str, StringConstraints(max_length=30)] | None = None
    notes: Annotated[str, StringConstraints(max_length=500)] | None = None
    payment_method: PaymentMethod
    items: Annotated[list[OrderItemIn], Field(min_length=1, max_length=_s.max_order_lines)]
    expected_total: MoneyIn | None = Field(
        default=None,
        description="Total que el cliente revisó (el `total` de POST /api/public/orders/quote). Si se envía y no "
        "coincide con el total calculado ahora por el servidor → 409 price_changed (con la cotización actual).",
    )
    website: str | None = Field(
        default=None, description="Honeypot anti-bot: los clientes reales lo dejan vacío / no lo envían."
    )

    @field_validator("customer_phone")
    @classmethod
    def check_phone(cls, v: str | None) -> str | None:
        return _validate_phone(v)


class OrderQuoteIn(BaseModel):
    # Same item rules as OrderCreate (prices/totals sent by the client are ignored).
    model_config = ConfigDict(extra="ignore")
    items: Annotated[list[OrderItemIn], Field(min_length=1, max_length=_s.max_order_lines)]


class AdminOrderCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    customer_name: CustomerName
    customer_phone: Annotated[str, StringConstraints(max_length=30)] | None = None
    notes: Annotated[str, StringConstraints(max_length=500)] | None = None
    payment_method: PaymentMethod
    items: Annotated[list[OrderItemIn], Field(min_length=1, max_length=_s.max_order_lines)]
    confirm: bool = Field(default=True, description="true: el pedido nace 'confirmed' (venta en mostrador)")

    @field_validator("customer_phone")
    @classmethod
    def check_phone(cls, v: str | None) -> str | None:
        return _validate_phone(v)


class StatusChangeIn(InModel):
    status: Literal["confirmed", "preparing", "ready", "delivered"]
    note: OptionalNote = None


class CancelIn(InModel):
    reason: Reason


class CreditAuthorizeIn(InModel):
    note: OptionalNote = None


class CreditRejectIn(InModel):
    reason: Reason


class RegisterPaymentIn(InModel):
    method: Literal["cash", "transfer"]
    amount: PositiveMoneyIn
    reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    note: OptionalNote = None


class ApproveProofIn(InModel):
    amount: PositiveMoneyIn | None = Field(
        default=None, description="Importe verificado. Si se omite se usa declared_amount."
    )
    note: OptionalNote = None
    allow_duplicate: bool = Field(
        default=False,
        description="Confirmación explícita para aprobar una imagen idéntica (mismo sha256) a otro comprobante "
        "ya aprobado. Sin ella la API responde 409 duplicate_proof.",
    )


class LinkCustomerIn(InModel):
    customer_id: uuid.UUID
    note: OptionalNote = None


class RejectProofIn(InModel):
    reason: Reason


class RefundIn(InModel):
    reason: Reason


class WriteOffIn(InModel):
    amount: PositiveMoneyIn | None = Field(default=None, description="Si se omite se condona todo el saldo.")
    reason: Reason


class CustomerCreate(InModel):
    name: CustomerName
    phone: Annotated[str, StringConstraints(max_length=30)] | None = None
    notes: Annotated[str, StringConstraints(max_length=1000)] | None = None

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v: str | None) -> str | None:
        return _validate_phone(v)


class CustomerUpdate(InModel):
    name: CustomerName | None = None
    phone: Annotated[str, StringConstraints(max_length=30)] | None = None
    notes: Annotated[str, StringConstraints(max_length=1000)] | None = None

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v: str | None) -> str | None:
        return _validate_phone(v)


# --------------------------------------------------------------------------- output


class OrderItemOut(ORMModel):
    product_id: uuid.UUID
    product_name: str
    unit_price: Money
    quantity: int
    line_total: Money


class QuoteLineOut(BaseModel):
    product_id: uuid.UUID
    product_name: str
    unit_price: Money = Field(description="Precio actual del servidor")
    quantity: int = Field(description="Cantidad (líneas repetidas del mismo producto se suman)")
    line_total: Money


class QuoteErrorOut(BaseModel):
    product_id: uuid.UUID
    product_name: str | None = Field(description="null si el producto no existe")
    code: Literal["product_not_found", "product_unavailable"]
    detail: str


class OrderQuoteOut(BaseModel):
    lines: list[QuoteLineOut] = Field(description="Líneas válidas, en el orden enviado")
    errors: list[QuoteErrorOut] = Field(
        description="Productos que no se pueden pedir (inexistentes, inactivos o agotados). Si no está vacío, "
        "crear el pedido con estos ítems fallaría con 422."
    )
    total: Money = Field(description="Suma de line_total de las líneas válidas")
    is_valid: bool = Field(description="true si errors está vacío")


class PriceChangedErrorOut(ErrorOut):
    quote: OrderQuoteOut | None = Field(
        default=None, description="Solo en `409 price_changed`: la cotización actual del servidor."
    )


class PublicProofOut(ORMModel):
    id: uuid.UUID
    status: ProofStatus
    declared_amount: Money
    created_at: datetime
    rejection_reason: str | None


class PublicOrderOut(ORMModel):
    code: str
    status: OrderStatus
    payment_status: PaymentStatus
    credit_status: CreditStatus
    payment_method: PaymentMethod
    customer_name: str
    notes: str | None
    items: list[OrderItemOut]
    total: Money
    amount_paid: Money
    balance_due: Money
    created_at: datetime
    updated_at: datetime
    proofs: list[PublicProofOut]
    pending_proofs_total: Money = Field(
        description="Suma declarada de comprobantes en revisión. Lo que falta por transferir es "
        "balance_due - pending_proofs_total."
    )


class PublicOrderCreatedOut(BaseModel):
    tracking_token: str = Field(description="Secreto: se entrega UNA sola vez. Guárdalo para consultar el pedido.")
    order: PublicOrderOut


class OrderSummaryOut(ORMModel):
    id: uuid.UUID
    code: str
    customer_id: uuid.UUID
    customer_name: str
    customer_phone: str | None
    source: str
    status: OrderStatus
    payment_status: PaymentStatus
    credit_status: CreditStatus
    payment_method: PaymentMethod
    total: Money
    amount_paid: Money
    amount_written_off: Money
    balance_due: Money
    items_count: int
    pending_proofs_count: int
    created_at: datetime
    updated_at: datetime


class PaymentOut(ORMModel):
    id: uuid.UUID
    order_id: uuid.UUID
    order_code: str
    method: PaymentMethod
    amount: Money
    status: PaymentRecordStatus
    proof_id: uuid.UUID | None
    cash_session_id: uuid.UUID | None
    reference: str | None
    note: str | None
    received_by_id: uuid.UUID | None
    received_by_name: str | None
    created_at: datetime
    refunded_at: datetime | None
    refunded_by_name: str | None
    refund_reason: str | None


class ProofOut(ORMModel):
    id: uuid.UUID
    order_id: uuid.UUID
    order_code: str
    status: ProofStatus
    declared_amount: Money
    mime_type: str
    size_bytes: int
    original_filename: str | None
    sha256: str
    duplicate_of_proof_ids: list[uuid.UUID] = Field(
        description="Otros comprobantes pendientes o aprobados (de cualquier pedido) con la MISMA imagen. "
        "Si no está vacío, el admin debe revisar antes de aprobar."
    )
    file_url: str
    payment_id: uuid.UUID | None
    created_at: datetime
    reviewed_at: datetime | None
    reviewed_by_name: str | None
    rejection_reason: str | None
    review_note: str | None


class StatusHistoryOut(ORMModel):
    from_status: OrderStatus | None
    to_status: OrderStatus
    note: str | None
    changed_by_name: str | None
    created_at: datetime


class WriteOffOut(ORMModel):
    id: uuid.UUID
    order_id: uuid.UUID
    amount: Money
    reason: str
    created_by_name: str | None
    created_at: datetime


class CustomerMatchOut(ORMModel):
    id: uuid.UUID
    name: str
    phone: str | None
    is_verified: bool


class OrderDetailOut(OrderSummaryOut):
    notes: str | None
    items: list[OrderItemOut]
    payments: list[PaymentOut]
    proofs: list[ProofOut]
    status_history: list[StatusHistoryOut]
    writeoffs: list[WriteOffOut]
    credit_note: str | None
    credit_reviewed_at: datetime | None
    cancel_reason: str | None
    cancelled_at: datetime | None
    delivered_at: datetime | None
    is_debt: bool
    customer_verified: bool = Field(
        description="false: el cliente se creó desde un pedido web anónimo (teléfono no verificado)."
    )
    possible_customer_matches: list[CustomerMatchOut] = Field(
        description="Otros clientes con el mismo teléfono. El pedido NO se asocia solo: usa "
        "POST /api/admin/orders/{id}/customer para vincularlo."
    )
    allowed_transitions: list[OrderStatus] = Field(
        description="Estados que POST /status aceptaría ahora mismo (sin 'cancelled')."
    )
    can_cancel: bool


class AdminOrderCreatedOut(BaseModel):
    tracking_token: str
    order: OrderDetailOut


class PaymentResultOut(BaseModel):
    payment: PaymentOut
    order: OrderSummaryOut
    cash_movement_id: uuid.UUID | None


class ProofReviewOut(BaseModel):
    proof: ProofOut
    payment: PaymentOut | None
    order: OrderSummaryOut


class WriteOffResultOut(BaseModel):
    writeoff: WriteOffOut
    order: OrderSummaryOut


class DebtOut(BaseModel):
    order_id: uuid.UUID
    order_code: str
    customer_id: uuid.UUID
    customer_name: str
    customer_phone: str | None
    order_status: OrderStatus
    credit_status: CreditStatus
    total: Money
    amount_paid: Money
    amount_written_off: Money
    balance_due: Money
    created_at: datetime
    age_days: int
    last_payment_at: datetime | None


class CustomerDebtSummaryOut(BaseModel):
    customer_id: uuid.UUID
    customer_name: str
    customer_phone: str | None
    debt_orders_count: int
    total_due: Money
    oldest_debt_at: datetime
    max_age_days: int


class CustomerListItemOut(BaseModel):
    id: uuid.UUID
    name: str
    phone: str | None
    is_verified: bool
    orders_count: int
    debt_total: Money
    last_order_at: datetime | None
    created_at: datetime


class CustomerOut(ORMModel):
    id: uuid.UUID
    name: str
    phone: str | None
    is_verified: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class CustomerDetailOut(CustomerOut):
    orders_count: int
    debt_total: Money
    debts: list[DebtOut]
    recent_orders: list[OrderSummaryOut]
    recent_payments: list[PaymentOut]
