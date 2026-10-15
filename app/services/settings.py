from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import PaymentMethod
from app.errors import AppError
from app.models import BusinessSettings, User
from app.schemas.catalog import Limits, PublicSettings, SettingsOut, SettingsUpdate
from app.services import audit
from app.services.storage import ALLOWED_MIME_TYPES
from app.timeutil import get_zone


def get_row(db: Session, *, lock: bool = False) -> BusinessSettings:
    stmt = select(BusinessSettings).where(BusinessSettings.id == 1)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(stmt)
    if row is None:  # migration seeds it; recreate defensively
        row = BusinessSettings(id=1, business_name="Mi negocio", timezone=get_settings().default_timezone)
        db.add(row)
        db.flush()
    return row


def enabled_methods(row: BusinessSettings) -> list[PaymentMethod]:
    out: list[PaymentMethod] = []
    if row.cash_enabled:
        out.append(PaymentMethod.cash)
    if row.transfer_enabled:
        out.append(PaymentMethod.transfer)
    if row.credit_enabled:
        out.append(PaymentMethod.credit)
    return out


def is_enabled(row: BusinessSettings, method: PaymentMethod) -> bool:
    return method in enabled_methods(row)


def to_out(row: BusinessSettings) -> SettingsOut:
    return SettingsOut(
        business_name=row.business_name,
        accepting_orders=row.accepting_orders,
        timezone=row.timezone,
        enabled_payment_methods=enabled_methods(row),
        allow_partial_payments=row.allow_partial_payments,
        bank_name=row.bank_name,
        bank_account_type=row.bank_account_type,
        bank_account_number=row.bank_account_number,
        bank_account_holder=row.bank_account_holder,
        bank_holder_id=row.bank_holder_id,
        transfer_instructions=row.transfer_instructions,
        updated_at=row.updated_at,
    )


def to_public(row: BusinessSettings) -> PublicSettings:
    s = get_settings()
    show_bank = row.transfer_enabled
    return PublicSettings(
        business_name=row.business_name,
        accepting_orders=row.accepting_orders,
        timezone=row.timezone,
        enabled_payment_methods=enabled_methods(row),
        allow_partial_payments=row.allow_partial_payments,
        bank_name=row.bank_name if show_bank else None,
        bank_account_type=row.bank_account_type if show_bank else None,
        bank_account_number=row.bank_account_number if show_bank else None,
        bank_account_holder=row.bank_account_holder if show_bank else None,
        bank_holder_id=row.bank_holder_id if show_bank else None,
        transfer_instructions=row.transfer_instructions if show_bank else None,
        limits=Limits(
            max_order_lines=s.max_order_lines,
            max_quantity_per_line=s.max_quantity_per_line,
            max_proof_bytes=s.max_upload_bytes,
            allowed_proof_mime_types=sorted(ALLOWED_MIME_TYPES),
            max_pending_proofs_per_order=s.max_pending_proofs_per_order,
        ),
    )


_TEXT_FIELDS = (
    "bank_name",
    "bank_account_type",
    "bank_account_number",
    "bank_account_holder",
    "bank_holder_id",
    "transfer_instructions",
)


def update(db: Session, data: SettingsUpdate, actor: User, ip: str | None) -> BusinessSettings:
    row = get_row(db, lock=True)
    changes: dict[str, object] = {}
    sent = data.model_fields_set

    for field in ("business_name", "accepting_orders", "allow_partial_payments", "timezone"):
        if field in sent:
            value = getattr(data, field)
            if value is None:
                raise AppError(422, "validation_error", f"{field} no puede ser null")
            if field == "timezone":
                get_zone(value)
            if getattr(row, field) != value:
                changes[field] = {"from": getattr(row, field), "to": value}
                setattr(row, field, value)
    for field in _TEXT_FIELDS:
        if field in sent:
            value = getattr(data, field) or None
            if getattr(row, field) != value:
                changes[field] = {"from": getattr(row, field), "to": value}
                setattr(row, field, value)
    if "enabled_payment_methods" in sent:
        methods = data.enabled_payment_methods
        if not methods:
            raise AppError(422, "payment_method_required", "Debe haber al menos un método de pago habilitado")
        before = [m.value for m in enabled_methods(row)]
        row.cash_enabled = PaymentMethod.cash in methods
        row.transfer_enabled = PaymentMethod.transfer in methods
        row.credit_enabled = PaymentMethod.credit in methods
        after = [m.value for m in enabled_methods(row)]
        if before != after:
            changes["enabled_payment_methods"] = {"from": before, "to": after}
    if changes:
        row.updated_by_id = actor.id
        audit.record(db, user=actor, action="settings_updated", entity_type="settings", details=changes, ip=ip)
    db.flush()
    return row
