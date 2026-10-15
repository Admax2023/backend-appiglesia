from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import (
    CashMovementType,
    CashSessionStatus,
    DenominationKind,
    MovementDirection,
    PaymentMethod,
    PaymentRecordStatus,
)
from app.errors import AppError, not_found
from app.models import CashCountLine, CashMovement, CashSession, Payment, User
from app.money import MAX_MONEY, ZERO, q
from app.schemas.cash import (
    CashMovementIn,
    CashSessionDetailOut,
    CashSessionOut,
    CashSummaryOut,
    CloseCashIn,
    CountLineOut,
)
from app.services import audit

OUT_TYPES = {CashMovementType.expense, CashMovementType.withdrawal, CashMovementType.refund_cash}
IN_TYPES = {CashMovementType.deposit, CashMovementType.sale_cash}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_open_session(db: Session, *, lock: bool = False) -> CashSession | None:
    stmt = select(CashSession).where(CashSession.status == CashSessionStatus.open)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalar(stmt)


def require_open_session(db: Session) -> CashSession:
    s = get_open_session(db, lock=True)
    if s is None:
        raise AppError(409, "no_open_cash_session", "No hay una caja abierta. Abre la caja para registrar efectivo.")
    return s


def get_session(db: Session, session_id: uuid.UUID, *, lock: bool = False) -> CashSession:
    stmt = select(CashSession).where(CashSession.id == session_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    s = db.scalar(stmt)
    if s is None:
        raise not_found("cash_session_not_found", "Caja no encontrada")
    return s


def open_session(db: Session, opening_amount: Decimal, note: str | None, actor: User, ip: str | None) -> CashSession:
    if get_open_session(db) is not None:
        raise AppError(409, "cash_session_already_open", "Ya hay una caja abierta")
    s = CashSession(status=CashSessionStatus.open, opening_amount=q(opening_amount), opening_note=note,
                    opened_by_id=actor.id, opened_at=_now())
    db.add(s)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise AppError(409, "cash_session_already_open", "Ya hay una caja abierta")
    audit.record(db, user=actor, action="cash_opened", entity_type="cash_session", entity_id=s.id,
                 details={"opening_amount": s.opening_amount}, ip=ip)
    return s


def summary(db: Session, s: CashSession) -> CashSummaryOut:
    db.flush()
    rows = db.execute(
        select(CashMovement.type, CashMovement.direction, func.coalesce(func.sum(CashMovement.amount), 0),
               func.count(CashMovement.id))
        .where(CashMovement.session_id == s.id)
        .group_by(CashMovement.type, CashMovement.direction)
    ).all()
    agg: dict[tuple[CashMovementType, MovementDirection], tuple[Decimal, int]] = {
        (t, d): (q(a), int(c)) for t, d, a, c in rows
    }

    def amt(t: CashMovementType, d: MovementDirection) -> Decimal:
        return agg.get((t, d), (ZERO, 0))[0]

    IN, OUT = MovementDirection.in_, MovementDirection.out
    total_in = sum((a for (t, d), (a, _) in agg.items() if d == IN), ZERO)
    total_out = sum((a for (t, d), (a, _) in agg.items() if d == OUT), ZERO)
    if s.status == CashSessionStatus.closed and s.transfer_income is not None:
        tr = (s.transfer_income, s.transfer_payments_count or 0)  # frozen at close
    else:
        tr = live_transfer_totals(db, s.opened_at, s.closed_at or _now())
    return CashSummaryOut(
        opening_amount=s.opening_amount,
        cash_sales=amt(CashMovementType.sale_cash, IN),
        cash_refunds=amt(CashMovementType.refund_cash, OUT),
        expenses=amt(CashMovementType.expense, OUT),
        withdrawals=amt(CashMovementType.withdrawal, OUT),
        deposits=amt(CashMovementType.deposit, IN),
        adjustments_in=amt(CashMovementType.adjustment, IN),
        adjustments_out=amt(CashMovementType.adjustment, OUT),
        total_in=q(total_in),
        total_out=q(total_out),
        expected_cash=q(s.opening_amount + total_in - total_out),
        cash_payments_count=agg.get((CashMovementType.sale_cash, IN), (ZERO, 0))[1],
        transfer_income=q(tr[0]),
        transfer_payments_count=int(tr[1]),
        movements_count=sum(c for (_, c) in agg.values()),
    )


def live_transfer_totals(db: Session, start: datetime, end: datetime) -> tuple[Decimal, int]:
    row = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0), func.count(Payment.id)).where(
            Payment.method == PaymentMethod.transfer,
            Payment.status == PaymentRecordStatus.completed,
            Payment.created_at >= start,
            Payment.created_at <= end,
        )
    ).one()
    return q(row[0]), int(row[1])


def detail(db: Session, s: CashSession) -> CashSessionDetailOut:
    lines = db.scalars(
        select(CashCountLine)
        .where(CashCountLine.session_id == s.id)
        .order_by(CashCountLine.kind, CashCountLine.denomination.desc())
    ).all()
    base = CashSessionOut.model_validate(s)
    return CashSessionDetailOut(
        **base.model_dump(), summary=summary(db, s), count_lines=[CountLineOut.model_validate(x) for x in lines]
    )


def add_movement_row(
    db: Session,
    s: CashSession,
    *,
    type_: CashMovementType,
    direction: MovementDirection,
    amount: Decimal,
    reason: str | None,
    actor: User,
    payment_id: uuid.UUID | None = None,
    refund_payment_id: uuid.UUID | None = None,
) -> CashMovement:
    """Caller must hold the session row lock (require_open_session)."""
    if s.status != CashSessionStatus.open:
        raise AppError(409, "cash_session_closed", "La caja está cerrada")
    if direction == MovementDirection.out:
        expected = summary(db, s).expected_cash
        if amount > expected:
            raise AppError(409, "insufficient_cash", f"No hay suficiente efectivo en caja (esperado {expected})")
    m = CashMovement(session_id=s.id, type=type_, direction=direction, amount=q(amount), reason=reason,
                     payment_id=payment_id, refund_payment_id=refund_payment_id, created_by_id=actor.id)
    db.add(m)
    db.flush()
    return m


def add_manual_movement(db: Session, data: CashMovementIn, actor: User, ip: str | None) -> CashMovement:
    t = CashMovementType(data.type)
    if t == CashMovementType.adjustment:
        if data.direction is None:
            raise AppError(422, "direction_required", "Indica la dirección del ajuste ('in' o 'out')")
        direction = data.direction
    else:
        direction = MovementDirection.out if t in OUT_TYPES else MovementDirection.in_
        if data.direction is not None and data.direction != direction:
            raise AppError(422, "invalid_direction", f"El tipo {t.value} siempre es '{direction.value}'")
    s = require_open_session(db)
    m = add_movement_row(db, s, type_=t, direction=direction, amount=data.amount, reason=data.reason, actor=actor)
    audit.record(db, user=actor, action="cash_movement_created", entity_type="cash_movement", entity_id=m.id,
                 details={"type": t, "direction": direction, "amount": m.amount, "reason": data.reason,
                          "session_id": s.id}, ip=ip)
    return m


def close_session(db: Session, session_id: uuid.UUID, data: CloseCashIn, actor: User, ip: str | None) -> CashSession:
    s = get_session(db, session_id, lock=True)
    if s.status == CashSessionStatus.closed:
        raise AppError(409, "cash_session_closed", "La caja ya está cerrada")
    cfg = get_settings()
    allowed = {
        DenominationKind.bill: {q(d) for d in cfg.bill_denominations},
        DenominationKind.coin: {q(d) for d in cfg.coin_denominations},
    }
    seen: set[tuple[DenominationKind, Decimal]] = set()
    lines: list[CashCountLine] = []
    counted = ZERO
    for c in data.counts:
        d = q(c.denomination)
        if d not in allowed[c.kind]:
            raise AppError(422, "invalid_denomination", f"Denominación no válida: {c.kind.value} {d}")
        if (c.kind, d) in seen:
            raise AppError(422, "duplicate_denomination", f"Denominación repetida: {c.kind.value} {d}")
        seen.add((c.kind, d))
        subtotal = q(d * c.quantity)
        counted += subtotal
        if counted > MAX_MONEY:
            raise AppError(422, "amount_too_large", "El conteo supera el máximo permitido: revisa las cantidades")
        lines.append(CashCountLine(session_id=s.id, kind=c.kind, denomination=d, quantity=c.quantity, subtotal=subtotal))
    expected = summary(db, s).expected_cash
    difference = q(counted - expected)
    note = (data.note or "").strip() or None
    if difference != ZERO and not note:
        raise AppError(
            422,
            "closing_note_required",
            f"Hay una diferencia de {difference}: escribe una observación para cerrar la caja",
        )
    db.add_all(lines)
    db.flush()
    closed_at = _now()
    s.transfer_income, s.transfer_payments_count = live_transfer_totals(db, s.opened_at, closed_at)
    s.status = CashSessionStatus.closed
    s.closed_at = closed_at
    s.closed_by_id = actor.id
    s.expected_cash = expected
    s.counted_cash = q(counted)
    s.difference = difference
    s.closing_note = note
    db.flush()
    audit.record(db, user=actor, action="cash_closed", entity_type="cash_session", entity_id=s.id,
                 details={"expected_cash": expected, "counted_cash": counted, "difference": difference,
                          "note": note}, ip=ip)
    return s


def list_sessions(
    db: Session, *, offset: int, limit: int, status: CashSessionStatus | None = None,
    start: datetime | None = None, end: datetime | None = None,
) -> tuple[list[CashSession], int]:
    conds = []
    if status:
        conds.append(CashSession.status == status)
    if start:
        conds.append(CashSession.opened_at >= start)
    if end:
        conds.append(CashSession.opened_at < end)
    total = db.scalar(select(func.count()).select_from(CashSession).where(*conds)) or 0
    rows = db.scalars(
        select(CashSession).where(*conds).order_by(CashSession.opened_at.desc()).offset(offset).limit(limit)
    ).all()
    return list(rows), total


def list_movements(db: Session, session_id: uuid.UUID, *, offset: int, limit: int) -> tuple[list[CashMovement], int]:
    get_session(db, session_id)
    total = db.scalar(select(func.count()).select_from(CashMovement).where(CashMovement.session_id == session_id)) or 0
    rows = db.scalars(
        select(CashMovement)
        .where(CashMovement.session_id == session_id)
        .order_by(CashMovement.created_at.desc(), CashMovement.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return list(rows), total
