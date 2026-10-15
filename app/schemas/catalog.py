"""Products, menu and business settings schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from app.enums import PaymentMethod
from app.money import Money, MoneyIn
from app.schemas.common import InModel, ORMModel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class ProductOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    price: Money
    is_active: bool
    is_available: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class ProductCreate(InModel):
    name: Name
    description: Description | None = None
    price: MoneyIn
    is_active: bool = True
    is_available: bool = True
    sort_order: Annotated[int, Field(ge=0, le=100000)] = 0


class ProductUpdate(InModel):
    """PATCH semantics: only the fields sent are changed."""

    name: Name | None = None
    description: Description | None = None
    price: MoneyIn | None = None
    is_available: bool | None = None
    sort_order: Annotated[int, Field(ge=0, le=100000)] | None = None


class PublicProduct(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    price: Money
    is_available: bool


class PublicMenu(ORMModel):
    business_name: str
    accepting_orders: bool
    currency: str = "COP"
    products: list[PublicProduct]


class Limits(ORMModel):
    max_order_lines: int
    max_quantity_per_line: int
    max_proof_bytes: int
    allowed_proof_mime_types: list[str]
    max_pending_proofs_per_order: int


class PublicSettings(ORMModel):
    business_name: str
    accepting_orders: bool
    currency: str = "COP"
    timezone: str
    enabled_payment_methods: list[PaymentMethod]
    allow_partial_payments: bool
    # bank details: null unless "transfer" is enabled
    bank_name: str | None
    bank_account_type: str | None
    bank_account_number: str | None
    bank_account_holder: str | None
    bank_holder_id: str | None
    transfer_instructions: str | None
    limits: Limits


class SettingsOut(ORMModel):
    business_name: str
    accepting_orders: bool
    currency: str = "COP"
    timezone: str
    enabled_payment_methods: list[PaymentMethod]
    allow_partial_payments: bool
    bank_name: str | None
    bank_account_type: str | None
    bank_account_number: str | None
    bank_account_holder: str | None
    bank_holder_id: str | None
    transfer_instructions: str | None
    updated_at: datetime


Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]


class SettingsUpdate(InModel):
    """PATCH semantics: only the fields sent are changed. Empty strings clear optional text fields."""

    business_name: Name | None = None
    accepting_orders: bool | None = None
    timezone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None = None
    enabled_payment_methods: Annotated[list[PaymentMethod], Field(min_length=1, max_length=3)] | None = None
    allow_partial_payments: bool | None = None
    bank_name: Short | None = None
    bank_account_type: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None
    bank_account_number: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None
    bank_account_holder: Short | None = None
    bank_holder_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] | None = None
    transfer_instructions: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None

    @field_validator("enabled_payment_methods")
    @classmethod
    def _unique(cls, v: list[PaymentMethod] | None) -> list[PaymentMethod] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("métodos de pago repetidos")
        return v
