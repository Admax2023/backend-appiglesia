"""SQLAlchemy 2.x ORM models (typed Mapped[]). Schema is created ONLY by Alembic migrations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    CashMovementType,
    CashSessionStatus,
    CreditStatus,
    DenominationKind,
    MovementDirection,
    OrderStatus,
    PaymentMethod,
    PaymentRecordStatus,
    PaymentStatus,
    ProofStatus,
    UserRole,
)

MONEY = Numeric(12, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pg_enum(enum_cls: type[PyEnum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def user_rel(fk: str) -> Mapped[User | None]:
    """Many-to-one to users for display names (read-only)."""
    return relationship("User", foreign_keys=fk, lazy="selectin", viewonly=True)


def _name(u: User | None) -> str | None:
    return u.full_name if u is not None else None


def created_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


# --------------------------------------------------------------------------- users / auth


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("username = lower(username)", name="username_lowercase"),
        CheckConstraint("length(username) >= 3", name="username_min_length"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), nullable=False, default=UserRole.admin
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family_id", "family_id"), Index("ix_refresh_tokens_user_id", "user_id"))

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # set when rotated (used) or revoked; a token with used_at set that is presented again => reuse
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship()


# --------------------------------------------------------------------------- settings


class BusinessSettings(Base):
    """Singleton row (id = 1)."""

    __tablename__ = "business_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("cash_enabled OR transfer_enabled OR credit_enabled", name="one_method_enabled"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    business_name: Mapped[str] = mapped_column(String(120), nullable=False)
    accepting_orders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    cash_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    transfer_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    credit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    allow_partial_payments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    bank_name: Mapped[str | None] = mapped_column(String(120))
    bank_account_type: Mapped[str | None] = mapped_column(String(60))
    bank_account_number: Mapped[str | None] = mapped_column(String(60))
    bank_account_holder: Mapped[str | None] = mapped_column(String(120))
    bank_holder_id: Mapped[str | None] = mapped_column(String(60))
    transfer_instructions: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Bogota")
    updated_at: Mapped[datetime] = updated_at_col()
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


# --------------------------------------------------------------------------- catalog


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_products_active_sort", "is_active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


# --------------------------------------------------------------------------- customers


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        Index(
            "uq_customers_phone_normalized",
            "phone_normalized",
            unique=True,
            postgresql_where=text("phone_normalized IS NOT NULL AND is_verified"),
        ),
        Index("ix_customers_phone_normalized", "phone_normalized"),
        Index("ix_customers_name", "name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(30))
    phone_normalized: Mapped[str | None] = mapped_column(String(20))
    # False for customer rows created from anonymous web orders: the phone was typed by an unknown
    # person, so it must not merge with (or put debt on) an existing customer. Only verified rows are
    # unique per phone; an admin links orders to the right customer explicitly.
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


# --------------------------------------------------------------------------- orders


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total >= 0", name="total_non_negative"),
        CheckConstraint("amount_paid >= 0", name="amount_paid_non_negative"),
        CheckConstraint("amount_written_off >= 0", name="written_off_non_negative"),
        CheckConstraint("amount_paid + amount_written_off <= total", name="no_overpayment"),
        CheckConstraint("balance_due >= 0", name="balance_non_negative"),
        CheckConstraint(
            "(payment_method = 'credit') = (credit_status <> 'none') "
            "OR credit_status IN ('authorized','rejected','not_needed')",
            name="credit_status_consistent",
        ),
        Index("ix_orders_created_at", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_payment_status", "payment_status"),
        Index("ix_orders_customer_id", "customer_id"),
        Index("ix_orders_customer_phone_created", "customer_phone_normalized", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)
    tracking_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_phone: Mapped[str | None] = mapped_column(String(30))
    customer_phone_normalized: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="web", server_default="web")
    payment_method: Mapped[PaymentMethod] = mapped_column(pg_enum(PaymentMethod, "payment_method"), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        pg_enum(OrderStatus, "order_status"), nullable=False, default=OrderStatus.pending_confirmation
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        pg_enum(PaymentStatus, "payment_status"), nullable=False, default=PaymentStatus.unpaid
    )
    credit_status: Mapped[CreditStatus] = mapped_column(
        pg_enum(CreditStatus, "credit_status"), nullable=False, default=CreditStatus.none
    )
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"), server_default="0")
    amount_written_off: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0"), server_default="0"
    )
    balance_due: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    credit_reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    credit_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credit_note: Mapped[str | None] = mapped_column(String(500))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    customer: Mapped[Customer] = relationship()
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.position"
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("line_total = unit_price * quantity", name="line_total_consistent"),
        UniqueConstraint("order_id", "product_id", name="uq_order_items_order_product"),
        Index("ix_order_items_product_id", "product_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    __table_args__ = (Index("ix_order_status_history_order_id", "order_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[OrderStatus | None] = mapped_column(pg_enum(OrderStatus, "order_status"))
    to_status: Mapped[OrderStatus] = mapped_column(pg_enum(OrderStatus, "order_status"), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()

    changed_by: Mapped[User | None] = user_rel("OrderStatusHistory.changed_by_id")

    @property
    def changed_by_name(self) -> str | None:
        return _name(self.changed_by)


# --------------------------------------------------------------------------- proofs & payments


class PaymentProof(Base):
    __tablename__ = "payment_proofs"
    __table_args__ = (
        CheckConstraint("declared_amount > 0", name="declared_amount_positive"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint(
            "mime_type IN ('image/jpeg','image/png','image/webp')", name="mime_type_allowed"
        ),
        CheckConstraint(
            "status <> 'rejected' OR (rejection_reason IS NOT NULL AND length(trim(rejection_reason)) > 0)",
            name="rejection_reason_required",
        ),
        CheckConstraint(
            "status = 'pending_verification' OR (reviewed_at IS NOT NULL)", name="reviewed_when_not_pending"
        ),
        Index("ix_payment_proofs_status", "status", "created_at"),
        Index("ix_payment_proofs_order_id", "order_id"),
        Index("ix_payment_proofs_sha256", "sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    storage_path: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    original_filename: Mapped[str | None] = mapped_column(String(150))
    mime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    declared_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[ProofStatus] = mapped_column(
        pg_enum(ProofStatus, "proof_status"), nullable=False, default=ProofStatus.pending_verification
    )
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    review_note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    reviewed_by: Mapped[User | None] = user_rel("PaymentProof.reviewed_by_id")
    order: Mapped[Order] = relationship(lazy="selectin", viewonly=True)
    payment: Mapped[Payment | None] = relationship(
        "Payment", primaryjoin="Payment.proof_id == PaymentProof.id", lazy="selectin", viewonly=True, uselist=False
    )

    @property
    def reviewed_by_name(self) -> str | None:
        return _name(self.reviewed_by)

    @property
    def order_code(self) -> str:
        return self.order.code

    @property
    def payment_id(self) -> uuid.UUID | None:
        return self.payment.id if self.payment is not None else None

    @property
    def file_url(self) -> str:
        return f"/api/admin/proofs/{self.id}/file"

    @property
    def duplicate_of_proof_ids(self) -> list[uuid.UUID]:
        """Other pending/approved proofs with the same image bytes (same sha256), any order."""
        from sqlalchemy import select
        from sqlalchemy.orm import object_session

        db = object_session(self)
        if db is None:
            return []
        return list(
            db.scalars(
                select(PaymentProof.id)
                .where(
                    PaymentProof.sha256 == self.sha256,
                    PaymentProof.id != self.id,
                    PaymentProof.status.in_([ProofStatus.pending_verification, ProofStatus.approved]),
                )
                .order_by(PaymentProof.created_at)
            ).all()
        )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("method IN ('cash','transfer')", name="method_is_money"),
        CheckConstraint("method <> 'cash' OR cash_session_id IS NOT NULL", name="cash_requires_session"),
        CheckConstraint("proof_id IS NULL OR method = 'transfer'", name="proof_only_transfer"),
        CheckConstraint(
            "status <> 'refunded' OR (refunded_at IS NOT NULL AND refund_reason IS NOT NULL)",
            name="refund_fields",
        ),
        Index("ix_payments_order_id", "order_id"),
        Index("ix_payments_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    method: Mapped[PaymentMethod] = mapped_column(pg_enum(PaymentMethod, "payment_method"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[PaymentRecordStatus] = mapped_column(
        pg_enum(PaymentRecordStatus, "payment_record_status"),
        nullable=False,
        default=PaymentRecordStatus.completed,
    )
    proof_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_proofs.id", ondelete="RESTRICT"), unique=True
    )
    cash_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_sessions.id", ondelete="RESTRICT")
    )
    reference: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))
    received_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    refund_reason: Mapped[str | None] = mapped_column(String(500))

    received_by: Mapped[User | None] = user_rel("Payment.received_by_id")
    refunded_by: Mapped[User | None] = user_rel("Payment.refunded_by_id")
    order: Mapped[Order] = relationship(lazy="selectin", viewonly=True)

    @property
    def received_by_name(self) -> str | None:
        return _name(self.received_by)

    @property
    def refunded_by_name(self) -> str | None:
        return _name(self.refunded_by)

    @property
    def order_code(self) -> str:
        return self.order.code


class DebtWriteOff(Base):
    __tablename__ = "debt_writeoffs"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_not_blank"),
        Index("ix_debt_writeoffs_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()

    created_by: Mapped[User | None] = user_rel("DebtWriteOff.created_by_id")

    @property
    def created_by_name(self) -> str | None:
        return _name(self.created_by)


# --------------------------------------------------------------------------- cash


class CashSession(Base):
    __tablename__ = "cash_sessions"
    __table_args__ = (
        CheckConstraint("opening_amount >= 0", name="opening_non_negative"),
        CheckConstraint(
            "status <> 'closed' OR (closed_at IS NOT NULL AND expected_cash IS NOT NULL "
            "AND counted_cash IS NOT NULL AND difference IS NOT NULL)",
            name="closed_fields",
        ),
        CheckConstraint("counted_cash IS NULL OR counted_cash >= 0", name="counted_non_negative"),
        CheckConstraint(
            "difference IS NULL OR difference = 0 OR (closing_note IS NOT NULL AND length(trim(closing_note)) > 0)",
            name="note_when_difference",
        ),
        # only ONE open session at a time
        Index(
            "uq_cash_sessions_single_open",
            "status",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_cash_sessions_opened_at", "opened_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[CashSessionStatus] = mapped_column(
        pg_enum(CashSessionStatus, "cash_session_status"), nullable=False, default=CashSessionStatus.open
    )
    opening_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    opening_note: Mapped[str | None] = mapped_column(String(500))
    opened_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    closed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_cash: Mapped[Decimal | None] = mapped_column(MONEY)
    counted_cash: Mapped[Decimal | None] = mapped_column(MONEY)
    difference: Mapped[Decimal | None] = mapped_column(MONEY)
    closing_note: Mapped[str | None] = mapped_column(String(1000))
    # Frozen at close so a later refund cannot change a closed session's report (NULL: legacy rows).
    transfer_income: Mapped[Decimal | None] = mapped_column(MONEY)
    transfer_payments_count: Mapped[int | None] = mapped_column(Integer)

    opened_by: Mapped[User | None] = user_rel("CashSession.opened_by_id")
    closed_by: Mapped[User | None] = user_rel("CashSession.closed_by_id")

    @property
    def opened_by_name(self) -> str | None:
        return _name(self.opened_by)

    @property
    def closed_by_name(self) -> str | None:
        return _name(self.closed_by)


class CashMovement(Base):
    __tablename__ = "cash_movements"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "(type IN ('sale_cash','deposit') AND direction = 'in') OR "
            "(type IN ('refund_cash','expense','withdrawal') AND direction = 'out') OR "
            "type = 'adjustment'",
            name="direction_matches_type",
        ),
        CheckConstraint("type <> 'sale_cash' OR payment_id IS NOT NULL", name="sale_has_payment"),
        CheckConstraint("type <> 'refund_cash' OR refund_payment_id IS NOT NULL", name="refund_has_payment"),
        CheckConstraint(
            "type IN ('sale_cash','refund_cash') OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name="manual_requires_reason",
        ),
        Index("ix_cash_movements_session_id", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[CashMovementType] = mapped_column(pg_enum(CashMovementType, "cash_movement_type"), nullable=False)
    direction: Mapped[MovementDirection] = mapped_column(
        pg_enum(MovementDirection, "movement_direction"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="RESTRICT"), unique=True
    )
    refund_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="RESTRICT"), unique=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()

    created_by: Mapped[User | None] = user_rel("CashMovement.created_by_id")

    @property
    def created_by_name(self) -> str | None:
        return _name(self.created_by)


class CashCountLine(Base):
    __tablename__ = "cash_count_lines"
    __table_args__ = (
        CheckConstraint("denomination > 0", name="denomination_positive"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("subtotal = denomination * quantity", name="subtotal_consistent"),
        UniqueConstraint("session_id", "kind", "denomination", name="uq_cash_count_lines_session_kind_denom"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_sessions.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[DenominationKind] = mapped_column(pg_enum(DenominationKind, "denomination_kind"), nullable=False)
    denomination: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


# --------------------------------------------------------------------------- audit & idempotency


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_action", "action"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()

    user: Mapped[User | None] = user_rel("AuditLog.user_id")

    @property
    def user_name(self) -> str | None:
        return _name(self.user)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_keys_scope_key"),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()
