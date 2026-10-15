"""Payments (cash/transfer registration, abonos), refunds, and proof review (approve/reject/refund)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import (
    CashMovementType,
    MovementDirection,
    OrderStatus,
    PaymentMethod,
    PaymentRecordStatus,
    ProofStatus,
)
from app.errors import AppError, not_found
from app.models import CashMovement, Order, Payment, PaymentProof, User
from app.money import q
from app.schemas.orders import RegisterPaymentIn
from app.services import audit, cash, ledger
from app.services import orders as orders_svc
from app.services import settings as settings_svc
from app.services import storage


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- payments


def register_payment(
    db: Session, order_id: uuid.UUID, data: RegisterPaymentIn, actor: User, ip: str | None
) -> tuple[Payment, Order, CashMovement | None]:
    order = ledger.lock_order(db, order_id)
    srow = settings_svc.get_row(db)
    amount = q(data.amount)
    ledger.validate_payment_amount(order, amount, srow)
    method = PaymentMethod(data.method)
    session = cash.require_open_session(db) if method == PaymentMethod.cash else None
    payment = Payment(
        id=uuid.uuid4(),
        order_id=order.id,
        method=method,
        amount=amount,
        status=PaymentRecordStatus.completed,
        cash_session_id=session.id if session else None,
        reference=data.reference or None,
        note=data.note or None,
        received_by_id=actor.id,
    )
    db.add(payment)
    db.flush()
    movement = None
    if session is not None:
        movement = cash.add_movement_row(
            db, session, type_=CashMovementType.sale_cash, direction=MovementDirection.in_, amount=amount,
            reason=f"Pago pedido {order.code}", actor=actor, payment_id=payment.id,
        )
    ledger.recompute_order_payment_state(db, order)
    audit.record(db, user=actor, action="payment_registered", entity_type="payment", entity_id=payment.id,
                 details={"order_code": order.code, "method": method, "amount": amount,
                          "balance_due": order.balance_due}, ip=ip)
    return payment, order, movement


def _lock_payment(db: Session, payment_id: uuid.UUID) -> Payment:
    p = db.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update().execution_options(populate_existing=True)
    )
    if p is None:
        raise not_found("payment_not_found", "Pago no encontrado")
    return p


def refund_payment(
    db: Session, payment_id: uuid.UUID, reason: str, actor: User, ip: str | None
) -> tuple[Payment, Order, CashMovement | None]:
    pre = db.get(Payment, payment_id)
    if pre is None:
        raise not_found("payment_not_found", "Pago no encontrado")
    order = ledger.lock_order(db, pre.order_id)  # lock order first (lock ordering)
    proof = None
    if pre.proof_id:
        proof = db.scalar(
            select(PaymentProof).where(PaymentProof.id == pre.proof_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    payment = _lock_payment(db, payment_id)
    if payment.status == PaymentRecordStatus.refunded:
        raise AppError(409, "payment_already_refunded", "El pago ya fue reembolsado")
    movement = None
    if payment.method == PaymentMethod.cash:
        session = cash.require_open_session(db)
        movement = cash.add_movement_row(
            db, session, type_=CashMovementType.refund_cash, direction=MovementDirection.out, amount=payment.amount,
            reason=f"Reembolso pedido {order.code}: {reason}"[:500], actor=actor, refund_payment_id=payment.id,
        )
    payment.status = PaymentRecordStatus.refunded
    payment.refunded_at = _now()
    payment.refunded_by_id = actor.id
    payment.refund_reason = reason
    if proof is not None:
        proof.status = ProofStatus.refunded
    db.flush()
    ledger.recompute_order_payment_state(db, order)
    audit.record(db, user=actor, action="payment_refunded", entity_type="payment", entity_id=payment.id,
                 details={"order_code": order.code, "method": payment.method, "amount": payment.amount,
                          "reason": reason, "proof_id": payment.proof_id}, ip=ip)
    return payment, order, movement


def get_payment(db: Session, payment_id: uuid.UUID) -> Payment:
    p = db.get(Payment, payment_id)
    if p is None:
        raise not_found("payment_not_found", "Pago no encontrado")
    return p


def list_payments(
    db: Session, *, offset: int, limit: int, method: PaymentMethod | None = None,
    status: PaymentRecordStatus | None = None, order_id: uuid.UUID | None = None,
    cash_session_id: uuid.UUID | None = None, start: datetime | None = None, end: datetime | None = None,
) -> tuple[list[Payment], int]:
    conds = []
    if method:
        conds.append(Payment.method == method)
    if status:
        conds.append(Payment.status == status)
    if order_id:
        conds.append(Payment.order_id == order_id)
    if cash_session_id:
        conds.append(Payment.cash_session_id == cash_session_id)
    if start:
        conds.append(Payment.created_at >= start)
    if end:
        conds.append(Payment.created_at < end)
    total = db.scalar(select(func.count()).select_from(Payment).where(*conds)) or 0
    rows = db.scalars(
        select(Payment).where(*conds).order_by(Payment.created_at.desc(), Payment.id).offset(offset).limit(limit)
    ).all()
    return list(rows), total


# --------------------------------------------------------------------------- proofs


def upload_proof(
    db: Session, token: str, data: bytes, filename: str | None, declared_amount: Decimal
) -> tuple[PaymentProof, Order, str]:
    img = storage.validate_image(data, filename)
    order = orders_svc.get_by_token(db, token, lock=True)
    srow = settings_svc.get_row(db)
    if order.status == OrderStatus.cancelled:
        raise AppError(409, "order_cancelled", "El pedido está cancelado")
    if not srow.transfer_enabled and order.payment_method != PaymentMethod.transfer:
        raise AppError(409, "transfer_not_enabled", "El pago por transferencia no está habilitado")
    pending, pending_total = db.execute(
        select(func.count(PaymentProof.id), func.coalesce(func.sum(PaymentProof.declared_amount), 0)).where(
            PaymentProof.order_id == order.id, PaymentProof.status == ProofStatus.pending_verification
        )
    ).one()
    if pending >= get_settings().max_pending_proofs_per_order:
        raise AppError(409, "too_many_pending_proofs",
                       "Ya hay comprobantes pendientes de verificación para este pedido")
    ledger.validate_payment_amount(order, q(declared_amount), srow)
    # Proofs still waiting for review already claim part of the balance (the order lock is held).
    remaining = q(order.balance_due - q(pending_total))
    if remaining <= 0:
        raise AppError(409, "pending_proofs_cover_balance",
                       "Los comprobantes en revisión ya cubren el saldo del pedido. Espera su verificación.")
    if q(declared_amount) > remaining:
        raise AppError(422, "amount_exceeds_balance",
                       f"El importe ({q(declared_amount)}) supera el saldo no cubierto por comprobantes "
                       f"en revisión ({remaining})")
    key = storage.save_proof(img)
    try:
        proof = PaymentProof(
            id=uuid.uuid4(), order_id=order.id, storage_path=key, original_filename=img.original_filename,
            mime_type=img.mime_type, size_bytes=img.size, sha256=img.sha256, declared_amount=q(declared_amount),
            status=ProofStatus.pending_verification,
        )
        db.add(proof)
        db.flush()
    except Exception:
        storage.delete_quietly(key)
        raise
    return proof, order, key


def get_proof(db: Session, proof_id: uuid.UUID) -> PaymentProof:
    p = db.get(PaymentProof, proof_id)
    if p is None:
        raise not_found("proof_not_found", "Comprobante no encontrado")
    return p


def _lock_order_and_proof(db: Session, proof_id: uuid.UUID) -> tuple[Order, PaymentProof]:
    pre = get_proof(db, proof_id)
    order = ledger.lock_order(db, pre.order_id)
    proof = db.scalar(
        select(PaymentProof).where(PaymentProof.id == proof_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    assert proof is not None
    return order, proof


def approve_proof(
    db: Session, proof_id: uuid.UUID, amount: Decimal | None, note: str | None, actor: User, ip: str | None,
    allow_duplicate: bool = False,
) -> tuple[PaymentProof, Payment, Order]:
    order, proof = _lock_order_and_proof(db, proof_id)
    if proof.status != ProofStatus.pending_verification:
        raise AppError(409, "proof_not_pending", f"El comprobante ya fue revisado ({proof.status.value})")
    approved_dups = [
        str(x) for x in db.scalars(
            select(PaymentProof.id).where(
                PaymentProof.sha256 == proof.sha256, PaymentProof.id != proof.id,
                PaymentProof.status == ProofStatus.approved,
            )
        ).all()
    ]
    if approved_dups and not allow_duplicate:
        raise AppError(
            409, "duplicate_proof",
            "La misma imagen ya se aprobó como pago en otro comprobante. Verifica la transferencia y, si es "
            "correcta, aprueba con allow_duplicate=true.",
        )
    srow = settings_svc.get_row(db)
    value = q(amount if amount is not None else proof.declared_amount)
    ledger.validate_payment_amount(order, value, srow)
    payment = Payment(
        id=uuid.uuid4(), order_id=order.id, method=PaymentMethod.transfer, amount=value,
        status=PaymentRecordStatus.completed, proof_id=proof.id, received_by_id=actor.id, note=note or None,
    )
    db.add(payment)
    proof.status = ProofStatus.approved
    proof.reviewed_at = _now()
    proof.reviewed_by_id = actor.id
    proof.review_note = note or None
    db.flush()
    ledger.recompute_order_payment_state(db, order)
    audit.record(db, user=actor, action="proof_approved", entity_type="payment_proof", entity_id=proof.id,
                 details={"order_code": order.code, "declared_amount": proof.declared_amount,
                          "approved_amount": value, "payment_id": payment.id,
                          "duplicate_of_approved_proof_ids": approved_dups}, ip=ip)
    return proof, payment, order


def reject_proof(
    db: Session, proof_id: uuid.UUID, reason: str, actor: User, ip: str | None
) -> tuple[PaymentProof, Order]:
    order, proof = _lock_order_and_proof(db, proof_id)
    if proof.status != ProofStatus.pending_verification:
        raise AppError(409, "proof_not_pending", f"El comprobante ya fue revisado ({proof.status.value})")
    proof.status = ProofStatus.rejected
    proof.rejection_reason = reason
    proof.reviewed_at = _now()
    proof.reviewed_by_id = actor.id
    db.flush()
    audit.record(db, user=actor, action="proof_rejected", entity_type="payment_proof", entity_id=proof.id,
                 details={"order_code": order.code, "declared_amount": proof.declared_amount, "reason": reason},
                 ip=ip)
    return proof, order


def refund_proof(
    db: Session, proof_id: uuid.UUID, reason: str, actor: User, ip: str | None
) -> tuple[PaymentProof, Payment, Order]:
    proof = get_proof(db, proof_id)
    if proof.status != ProofStatus.approved:
        raise AppError(409, "proof_not_approved", "Solo se pueden reembolsar comprobantes aprobados")
    payment_id = db.scalar(select(Payment.id).where(Payment.proof_id == proof.id))
    if payment_id is None:  # pragma: no cover - approved proofs always have a payment
        raise AppError(409, "proof_not_approved", "El comprobante no tiene pago asociado")
    payment, order, _ = refund_payment(db, payment_id, reason, actor, ip)
    db.refresh(proof)
    return proof, payment, order


def list_proofs(
    db: Session, *, offset: int, limit: int, status: ProofStatus | None = None, order_id: uuid.UUID | None = None
) -> tuple[list[PaymentProof], int]:
    conds = []
    if status:
        conds.append(PaymentProof.status == status)
    if order_id:
        conds.append(PaymentProof.order_id == order_id)
    total = db.scalar(select(func.count()).select_from(PaymentProof).where(*conds)) or 0
    order_by = (
        (PaymentProof.created_at.asc(),) if status == ProofStatus.pending_verification
        else (PaymentProof.created_at.desc(),)
    )
    rows = db.scalars(select(PaymentProof).where(*conds).order_by(*order_by, PaymentProof.id).offset(offset).limit(limit)).all()
    return list(rows), total

