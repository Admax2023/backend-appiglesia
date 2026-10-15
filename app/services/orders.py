from __future__ import annotations

import secrets
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import ORDER_TRANSITIONS, CreditStatus, OrderStatus, PaymentMethod, ProofStatus
from app.errors import AppError, not_found
from app.models import (
    Customer,
    DebtWriteOff,
    Order,
    OrderItem,
    OrderStatusHistory,
    Payment,
    PaymentProof,
    Product,
    User,
)
from app.money import MAX_MONEY, ZERO, q
from app.schemas.orders import (
    AdminOrderCreate,
    CustomerMatchOut,
    OrderCreate,
    OrderDetailOut,
    OrderItemOut,
    OrderQuoteIn,
    OrderQuoteOut,
    OrderSummaryOut,
    PaymentOut,
    ProofOut,
    PublicOrderOut,
    PublicProofOut,
    QuoteErrorOut,
    QuoteLineOut,
    StatusHistoryOut,
    WriteOffOut,
    normalize_phone,
)
from app.security import new_tracking_token, sha256_hex
from app.services import audit, ledger
from app.services import settings as settings_svc
from app.services.products import _escape_like

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L
CODE_PREFIX = "F-"
CODE_LEN = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_code(db: Session) -> str:
    for _ in range(20):
        code = CODE_PREFIX + "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if not db.scalar(select(exists().where(Order.code == code))):
            return code
    raise AppError(500, "code_generation_failed", "No se pudo generar el código del pedido")  # pragma: no cover


# --------------------------------------------------------------------------- creation


def _merge_items(items: list) -> "OrderedDict[uuid.UUID, int]":
    merged: OrderedDict[uuid.UUID, int] = OrderedDict()
    for it in items:
        merged[it.product_id] = merged.get(it.product_id, 0) + it.quantity
    max_q = get_settings().max_quantity_per_line
    for pid, qty in merged.items():
        if qty > max_q:
            raise AppError(422, "quantity_limit", f"Máximo {max_q} unidades por producto")
    return merged


def _price(db: Session, items: list) -> tuple["OrderedDict[uuid.UUID, int]", OrderQuoteOut]:
    """Single source of truth for pricing: merges lines, enforces quantity limits and prices every line
    from the DB (client prices are never read). Unknown/inactive/sold-out products are reported in
    `errors` (not raised) so the quote endpoint can show them; create_order turns them into 422."""
    merged = _merge_items(items)
    products = {
        p.id: p for p in db.scalars(select(Product).where(Product.id.in_(list(merged.keys())))).all()
    }
    lines: list[QuoteLineOut] = []
    errors: list[QuoteErrorOut] = []
    total = ZERO
    for pid, qty in merged.items():
        p = products.get(pid)
        if p is None:
            errors.append(QuoteErrorOut(product_id=pid, product_name=None, code="product_not_found",
                                        detail=f"Producto no encontrado: {pid}"))
            continue
        if not p.is_active or not p.is_available:
            errors.append(QuoteErrorOut(product_id=pid, product_name=p.name, code="product_unavailable",
                                        detail=f"El producto '{p.name}' no está disponible"))
            continue
        line_total = q(p.price * qty)
        total += line_total
        lines.append(QuoteLineOut(product_id=pid, product_name=p.name, unit_price=p.price, quantity=qty,
                                  line_total=line_total))
    return merged, OrderQuoteOut(lines=lines, errors=errors, total=q(total), is_valid=not errors)


def _check_max_total(priced: OrderQuoteOut) -> None:
    if priced.total > MAX_MONEY:
        raise AppError(422, "amount_too_large", "El total del pedido supera el máximo permitido")


def quote(db: Session, data: OrderQuoteIn) -> OrderQuoteOut:
    """Price a cart exactly as create_order would, without writing anything."""
    _merged, priced = _price(db, data.items)
    _check_max_total(priced)
    return priced


def _check_duplicate(db: Session, phone_norm: str | None, name: str, merged: dict[uuid.UUID, int]) -> None:
    window = get_settings().duplicate_order_window_seconds
    if window <= 0:
        return
    since = _now() - timedelta(seconds=window)
    conds = [Order.created_at >= since, Order.status != OrderStatus.cancelled, Order.source == "web"]
    if phone_norm:
        conds.append(Order.customer_phone_normalized == phone_norm)
    else:
        conds.append(Order.customer_phone_normalized.is_(None))
        conds.append(func.lower(Order.customer_name) == name.lower())
    recent_ids = db.scalars(select(Order.id).where(*conds)).all()
    if not recent_ids:
        return
    signature = sorted((str(k), v) for k, v in merged.items())
    rows = db.execute(
        select(OrderItem.order_id, OrderItem.product_id, OrderItem.quantity).where(OrderItem.order_id.in_(recent_ids))
    ).all()
    by_order: dict[uuid.UUID, list[tuple[str, int]]] = {}
    for oid, pid, qty in rows:
        by_order.setdefault(oid, []).append((str(pid), qty))
    for items in by_order.values():
        if sorted(items) == signature:
            raise AppError(
                409,
                "duplicate_order",
                "Ya recibimos un pedido idéntico hace unos momentos. Revisa el estado de tu pedido.",
            )


def create_order(
    db: Session,
    data: OrderCreate | AdminOrderCreate,
    *,
    actor: User | None,
    ip: str | None,
) -> tuple[Order, str]:
    from app.services import customers as customers_svc  # local import: avoid cycle

    is_admin = actor is not None
    srow = settings_svc.get_row(db)
    if not is_admin:
        assert isinstance(data, OrderCreate)
        if data.website:
            raise AppError(400, "spam_detected", "Solicitud rechazada")
        if not srow.accepting_orders:
            raise AppError(409, "orders_closed", "En este momento no estamos recibiendo pedidos")
        if not settings_svc.is_enabled(srow, data.payment_method):
            raise AppError(422, "payment_method_disabled", "El método de pago seleccionado no está habilitado")

    merged, priced = _price(db, data.items)
    if priced.errors:
        err = priced.errors[0]
        raise AppError(422, err.code, err.detail)
    _check_max_total(priced)
    if not is_admin and isinstance(data, OrderCreate) and data.expected_total is not None:
        if q(data.expected_total) != priced.total:
            raise AppError(
                409,
                "price_changed",
                "Los precios cambiaron desde que revisaste tu pedido. Revisa el nuevo total y confirma de nuevo.",
                extra={"quote": priced.model_dump(mode="json")},
            )

    phone_norm = normalize_phone(data.customer_phone)
    if not is_admin:
        # Serialize concurrent creations for the same customer identity so the duplicate check below
        # sees the other transaction's order (lock released at commit/rollback).
        ident = "dup:" + (phone_norm or data.customer_name.strip().lower())
        db.execute(select(func.pg_advisory_xact_lock(func.hashtext(ident))))
        _check_duplicate(db, phone_norm, data.customer_name, merged)

    customer = customers_svc.find_or_create(db, data.customer_name, data.customer_phone, verified=is_admin)
    token = new_tracking_token()
    order = Order(
        id=uuid.uuid4(),
        code=generate_code(db),
        tracking_token_hash=sha256_hex(token),
        customer_id=customer.id,
        customer_name=data.customer_name,
        customer_phone=data.customer_phone,
        customer_phone_normalized=phone_norm,
        notes=data.notes or None,
        source="admin" if is_admin else "web",
        payment_method=data.payment_method,
        status=OrderStatus.pending_confirmation,
        credit_status=(
            CreditStatus.pending_authorization if data.payment_method == PaymentMethod.credit else CreditStatus.none
        ),
        total=ZERO,
        balance_due=ZERO,
    )
    for pos, line in enumerate(priced.lines):
        order.items.append(
            OrderItem(product_id=line.product_id, position=pos, product_name=line.product_name,
                      unit_price=line.unit_price, quantity=line.quantity, line_total=line.line_total)
        )
    order.total = priced.total
    order.balance_due = priced.total
    db.add(order)
    db.flush()

    initial = OrderStatus.pending_confirmation
    if is_admin and isinstance(data, AdminOrderCreate) and data.confirm:
        initial = OrderStatus.confirmed
        if order.credit_status == CreditStatus.pending_authorization:
            order.credit_status = CreditStatus.authorized
            order.credit_reviewed_by_id = actor.id
            order.credit_reviewed_at = _now()
        order.status = initial
    db.add(OrderStatusHistory(order_id=order.id, from_status=None, to_status=initial,
                              changed_by_id=actor.id if actor else None,
                              note="Pedido creado" + (" en mostrador" if is_admin else " desde la web")))
    ledger.recompute_order_payment_state(db, order)
    if is_admin:
        audit.record(db, user=actor, action="order_created", entity_type="order", entity_id=order.id,
                     details={"code": order.code, "total": order.total}, ip=ip)
    return order, token


# --------------------------------------------------------------------------- read models


def _items_count_sq():
    return (
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .where(OrderItem.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )


def _pending_proofs_sq():
    return (
        select(func.count(PaymentProof.id))
        .where(PaymentProof.order_id == Order.id, PaymentProof.status == ProofStatus.pending_verification)
        .correlate(Order)
        .scalar_subquery()
    )


def _summary(order: Order, items_count: int, pending: int) -> OrderSummaryOut:
    return OrderSummaryOut(
        id=order.id,
        code=order.code,
        customer_id=order.customer_id,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        source=order.source,
        status=order.status,
        payment_status=order.payment_status,
        credit_status=order.credit_status,
        payment_method=order.payment_method,
        total=order.total,
        amount_paid=order.amount_paid,
        amount_written_off=order.amount_written_off,
        balance_due=order.balance_due,
        items_count=int(items_count or 0),
        pending_proofs_count=int(pending or 0),
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def summary(db: Session, order: Order) -> OrderSummaryOut:
    db.flush()
    ic = db.scalar(select(func.coalesce(func.sum(OrderItem.quantity), 0)).where(OrderItem.order_id == order.id))
    pc = db.scalar(
        select(func.count(PaymentProof.id)).where(
            PaymentProof.order_id == order.id, PaymentProof.status == ProofStatus.pending_verification
        )
    )
    return _summary(order, ic or 0, pc or 0)


def list_orders(
    db: Session,
    *,
    offset: int,
    limit: int,
    status: list[OrderStatus] | None = None,
    payment_status: list | None = None,
    payment_method: PaymentMethod | None = None,
    credit_status: CreditStatus | None = None,
    customer_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    search: str | None = None,
    has_pending_proofs: bool | None = None,
) -> tuple[list[OrderSummaryOut], int]:
    conds = []
    if status:
        conds.append(Order.status.in_(status))
    if payment_status:
        conds.append(Order.payment_status.in_(payment_status))
    if payment_method:
        conds.append(Order.payment_method == payment_method)
    if credit_status:
        conds.append(Order.credit_status == credit_status)
    if customer_id:
        conds.append(Order.customer_id == customer_id)
    if start:
        conds.append(Order.created_at >= start)
    if end:
        conds.append(Order.created_at < end)
    if search:
        s = search.strip()
        pat = f"%{_escape_like(s)}%"
        ors = [Order.code.ilike(pat, escape="\\"), Order.customer_name.ilike(pat, escape="\\")]
        digits = normalize_phone(s)
        if digits and len(digits) >= 3:
            ors.append(Order.customer_phone_normalized.like(f"%{digits}%"))
        conds.append(or_(*ors))
    pending_sq = _pending_proofs_sq()
    if has_pending_proofs is True:
        conds.append(pending_sq > 0)
    elif has_pending_proofs is False:
        conds.append(pending_sq == 0)
    total = db.scalar(select(func.count()).select_from(Order).where(*conds)) or 0
    stmt: Select = (
        select(Order, _items_count_sq(), pending_sq)
        .where(*conds)
        .order_by(Order.created_at.desc(), Order.id)
        .offset(offset)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    return [_summary(o, ic, pc) for o, ic, pc in rows], total


def get_order(db: Session, order_id: uuid.UUID) -> Order:
    o = db.get(Order, order_id)
    if o is None:
        raise not_found("order_not_found", "Pedido no encontrado")
    return o


def detail(db: Session, order: Order) -> OrderDetailOut:
    from app.services import customers as customers_svc  # local import: avoid cycle

    s = summary(db, order)
    db.refresh(order, ["customer"])
    payments = db.scalars(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at)).all()
    proofs = db.scalars(
        select(PaymentProof).where(PaymentProof.order_id == order.id).order_by(PaymentProof.created_at)
    ).all()
    history = db.scalars(
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order.id)
        .order_by(OrderStatusHistory.created_at, OrderStatusHistory.id)
    ).all()
    writeoffs = db.scalars(
        select(DebtWriteOff).where(DebtWriteOff.order_id == order.id).order_by(DebtWriteOff.created_at)
    ).all()
    items = db.scalars(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.position)).all()
    return OrderDetailOut(
        **s.model_dump(),
        notes=order.notes,
        items=[OrderItemOut.model_validate(i) for i in items],
        payments=[PaymentOut.model_validate(p) for p in payments],
        proofs=[ProofOut.model_validate(p) for p in proofs],
        status_history=[StatusHistoryOut.model_validate(h) for h in history],
        writeoffs=[WriteOffOut.model_validate(w) for w in writeoffs],
        credit_note=order.credit_note,
        credit_reviewed_at=order.credit_reviewed_at,
        cancel_reason=order.cancel_reason,
        cancelled_at=order.cancelled_at,
        delivered_at=order.delivered_at,
        is_debt=ledger.is_debt(order),
        customer_verified=order.customer.is_verified,
        possible_customer_matches=[
            CustomerMatchOut.model_validate(c) for c in customers_svc.possible_matches(db, order.customer)
        ],
        allowed_transitions=allowed_transitions(order),
        can_cancel=OrderStatus.cancelled in ORDER_TRANSITIONS[order.status] and order.amount_paid <= ZERO,
    )


def allowed_transitions(order: Order) -> list[OrderStatus]:
    """Status changes POST /status would accept right now (same rules as change_status)."""
    if order.credit_status == CreditStatus.pending_authorization:
        return []
    out = []
    for st in OrderStatus:  # enum order = workflow order
        if st == OrderStatus.cancelled or st not in ORDER_TRANSITIONS[order.status]:
            continue
        if st == OrderStatus.delivered and order.balance_due > ZERO and order.credit_status != CreditStatus.authorized:
            continue
        out.append(st)
    return out


def get_by_token(db: Session, token: str, *, lock: bool = False) -> Order:
    if not token or len(token) > 128:
        raise not_found("order_not_found", "Pedido no encontrado")
    stmt = select(Order).where(Order.tracking_token_hash == sha256_hex(token))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    o = db.scalar(stmt)
    if o is None:
        raise not_found("order_not_found", "Pedido no encontrado")
    return o


def public_view(db: Session, order: Order) -> PublicOrderOut:
    db.flush()
    items = db.scalars(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.position)).all()
    proofs = db.scalars(
        select(PaymentProof).where(PaymentProof.order_id == order.id).order_by(PaymentProof.created_at)
    ).all()
    return PublicOrderOut(
        code=order.code,
        status=order.status,
        payment_status=order.payment_status,
        credit_status=order.credit_status,
        payment_method=order.payment_method,
        customer_name=order.customer_name,
        notes=order.notes,
        items=[OrderItemOut.model_validate(i) for i in items],
        total=order.total,
        amount_paid=order.amount_paid,
        balance_due=order.balance_due,
        created_at=order.created_at,
        updated_at=order.updated_at,
        proofs=[PublicProofOut.model_validate(p) for p in proofs],
        pending_proofs_total=q(
            sum((p.declared_amount for p in proofs if p.status == ProofStatus.pending_verification), ZERO)
        ),
    )


# --------------------------------------------------------------------------- state changes


def _history(db: Session, order: Order, old: OrderStatus, new: OrderStatus, actor: User, note: str | None) -> None:
    db.add(OrderStatusHistory(order_id=order.id, from_status=old, to_status=new, changed_by_id=actor.id, note=note))


def change_status(
    db: Session, order_id: uuid.UUID, new_status: OrderStatus, note: str | None, actor: User, ip: str | None
) -> Order:
    order = ledger.lock_order(db, order_id)
    old = order.status
    if new_status == old:
        raise AppError(409, "invalid_transition", f"El pedido ya está en estado {old.value}")
    if new_status == OrderStatus.cancelled:
        raise AppError(422, "use_cancel_endpoint", "Para cancelar usa POST /api/admin/orders/{id}/cancel")
    if new_status not in ORDER_TRANSITIONS[old]:
        raise AppError(409, "invalid_transition", f"Transición no permitida: {old.value} → {new_status.value}")
    if order.credit_status == CreditStatus.pending_authorization:
        raise AppError(409, "credit_authorization_required", "El fiado de este pedido aún no está autorizado")
    if new_status == OrderStatus.delivered:
        if order.balance_due > ZERO and order.credit_status != CreditStatus.authorized:
            raise AppError(
                409,
                "balance_due_requires_credit",
                "El pedido tiene saldo pendiente: registra el pago o autoriza el fiado antes de entregarlo",
            )
        order.delivered_at = _now()
    order.status = new_status
    _history(db, order, old, new_status, actor, note)
    ledger.recompute_order_payment_state(db, order)
    if new_status == OrderStatus.delivered:
        audit.record(db, user=actor, action="order_delivered", entity_type="order", entity_id=order.id,
                     details={"code": order.code, "balance_due": order.balance_due}, ip=ip)
    return order


def cancel(db: Session, order_id: uuid.UUID, reason: str, actor: User, ip: str | None) -> Order:
    order = ledger.lock_order(db, order_id)
    old = order.status
    if OrderStatus.cancelled not in ORDER_TRANSITIONS[old]:
        raise AppError(409, "invalid_transition", f"No se puede cancelar un pedido en estado {old.value}")
    if order.amount_paid > ZERO:
        raise AppError(409, "order_has_payments", "El pedido tiene pagos aprobados: reembólsalos antes de cancelar")
    pending = db.scalars(
        select(PaymentProof)
        .where(PaymentProof.order_id == order.id, PaymentProof.status == ProofStatus.pending_verification)
        .with_for_update()
    ).all()
    now = _now()
    for p in pending:
        p.status = ProofStatus.rejected
        p.rejection_reason = f"Pedido cancelado: {reason}"[:500]
        p.reviewed_at = now
        p.reviewed_by_id = actor.id
    order.status = OrderStatus.cancelled
    order.cancel_reason = reason
    order.cancelled_at = now
    if order.credit_status == CreditStatus.pending_authorization:
        order.credit_status = CreditStatus.rejected
    _history(db, order, old, OrderStatus.cancelled, actor, reason)
    ledger.recompute_order_payment_state(db, order)
    audit.record(db, user=actor, action="order_cancelled", entity_type="order", entity_id=order.id,
                 details={"code": order.code, "reason": reason, "from_status": old,
                          "auto_rejected_proofs": len(pending)}, ip=ip)
    return order


def authorize_credit(db: Session, order_id: uuid.UUID, note: str | None, actor: User, ip: str | None) -> Order:
    order = ledger.lock_order(db, order_id)
    if order.status == OrderStatus.cancelled:
        raise AppError(409, "order_cancelled", "El pedido está cancelado")
    if order.credit_status == CreditStatus.authorized:
        raise AppError(409, "credit_already_authorized", "El fiado ya está autorizado")
    if order.balance_due <= ZERO:
        raise AppError(409, "order_already_paid", "El pedido no tiene saldo pendiente")
    old_credit = order.credit_status
    order.credit_status = CreditStatus.authorized
    order.credit_reviewed_by_id = actor.id
    order.credit_reviewed_at = _now()
    order.credit_note = note
    if order.status == OrderStatus.pending_confirmation:
        _history(db, order, order.status, OrderStatus.confirmed, actor, "Fiado autorizado")
        order.status = OrderStatus.confirmed
    ledger.recompute_order_payment_state(db, order)
    audit.record(db, user=actor, action="credit_authorized", entity_type="order", entity_id=order.id,
                 details={"code": order.code, "balance_due": order.balance_due, "from": old_credit, "note": note},
                 ip=ip)
    return order


def reject_credit(db: Session, order_id: uuid.UUID, reason: str, actor: User, ip: str | None) -> Order:
    order = ledger.lock_order(db, order_id)
    if order.credit_status != CreditStatus.pending_authorization:
        raise AppError(409, "credit_not_pending", "El pedido no tiene un fiado pendiente de autorización")
    order.credit_status = CreditStatus.rejected
    order.credit_reviewed_by_id = actor.id
    order.credit_reviewed_at = _now()
    order.credit_note = reason
    db.flush()
    audit.record(db, user=actor, action="credit_rejected", entity_type="order", entity_id=order.id,
                 details={"code": order.code, "reason": reason}, ip=ip)
    return order



def link_customer(
    db: Session, order_id: uuid.UUID, customer_id: uuid.UUID, note: str | None, actor: User, ip: str | None
) -> Order:
    """Re-assign an order (and therefore its debt) to another customer. Admin-only, audited.

    An unverified customer row left without orders is deleted (it only existed for that order).
    """
    order = ledger.lock_order(db, order_id)
    target = db.get(Customer, customer_id)
    if target is None:
        raise not_found("customer_not_found", "Cliente no encontrado")
    if order.customer_id == target.id:
        raise AppError(409, "customer_already_linked", "El pedido ya está asociado a ese cliente")
    old = db.get(Customer, order.customer_id)
    assert old is not None
    order.customer_id = target.id
    db.flush()
    removed_old = False
    if not old.is_verified and not db.scalar(select(exists().where(Order.customer_id == old.id))):
        db.delete(old)
        db.flush()
        removed_old = True
    audit.record(db, user=actor, action="order_customer_linked", entity_type="order", entity_id=order.id,
                 details={"code": order.code, "order_customer_name": order.customer_name,
                          "from_customer_id": old.id, "from_customer_name": old.name,
                          "from_verified": old.is_verified, "removed_unverified_customer": removed_old,
                          "to_customer_id": target.id, "to_customer_name": target.name,
                          "to_verified": target.is_verified, "balance_due": order.balance_due, "note": note},
                 ip=ip)
    db.expire(order, ["customer"])
    return order
