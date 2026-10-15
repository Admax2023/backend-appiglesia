from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError, not_found
from app.models import Customer, Order, Payment, User
from app.money import ZERO
from app.schemas.orders import (
    CustomerCreate,
    CustomerDetailOut,
    CustomerListItemOut,
    CustomerUpdate,
    PaymentOut,
    normalize_phone,
)
from app.services import audit, debts, orders as orders_svc
from app.services.products import _escape_like


def find_or_create(db: Session, name: str, phone: str | None, *, verified: bool) -> Customer:
    """Customer for a new order.

    verified=False (anonymous web order): ALWAYS a new, unverified customer row. A phone number typed
    by an unknown person proves nothing, so it must never attach the order (or a fiado debt) to an
    existing customer; an admin links it explicitly (POST /api/admin/orders/{id}/customer).

    verified=True (counter sale by an admin): reuse the verified customer with that phone, or create it.
    """
    norm = normalize_phone(phone)
    if norm is None or not verified:
        c = Customer(name=name, phone=phone, phone_normalized=norm, is_verified=verified)
        db.add(c)
        db.flush()
        return c
    db.execute(
        pg_insert(Customer)
        .values(id=uuid.uuid4(), name=name, phone=phone, phone_normalized=norm, is_verified=True)
        .on_conflict_do_nothing(
            index_elements=["phone_normalized"], index_where=text("phone_normalized IS NOT NULL AND is_verified")
        )
    )
    c = db.scalar(select(Customer).where(Customer.phone_normalized == norm, Customer.is_verified.is_(True)))
    assert c is not None
    return c


def _verified_phone_owner(db: Session, norm: str | None, exclude_id: uuid.UUID | None = None) -> uuid.UUID | None:
    if not norm:
        return None
    conds = [Customer.phone_normalized == norm, Customer.is_verified.is_(True)]
    if exclude_id is not None:
        conds.append(Customer.id != exclude_id)
    return db.scalar(select(Customer.id).where(*conds).limit(1))


def possible_matches(db: Session, customer: Customer) -> list[Customer]:
    """Other customers with the same normalized phone (candidates for an explicit admin link)."""
    if not customer.phone_normalized:
        return []
    return list(
        db.scalars(
            select(Customer)
            .where(Customer.phone_normalized == customer.phone_normalized, Customer.id != customer.id)
            .order_by(Customer.is_verified.desc(), Customer.created_at)
            .limit(10)
        ).all()
    )


def verify(db: Session, customer_id: uuid.UUID, actor: User, ip: str | None) -> Customer:
    c = db.scalar(select(Customer).where(Customer.id == customer_id).with_for_update())
    if c is None:
        raise not_found("customer_not_found", "Cliente no encontrado")
    if c.is_verified:
        raise AppError(409, "customer_already_verified", "El cliente ya está verificado")
    if _verified_phone_owner(db, c.phone_normalized, c.id):
        raise AppError(
            409, "phone_taken",
            "Ya existe un cliente verificado con ese teléfono: vincula los pedidos a ese cliente",
        )
    c.is_verified = True
    try:
        db.flush()
    except IntegrityError:
        raise AppError(409, "phone_taken", "Ya existe un cliente verificado con ese teléfono")
    audit.record(db, user=actor, action="customer_verified", entity_type="customer", entity_id=c.id,
                 details={"name": c.name, "phone": c.phone}, ip=ip)
    return c


def get(db: Session, customer_id: uuid.UUID) -> Customer:
    c = db.get(Customer, customer_id)
    if c is None:
        raise not_found("customer_not_found", "Cliente no encontrado")
    return c


def list_customers(
    db: Session, *, offset: int, limit: int, search: str | None = None, has_debt: bool | None = None
) -> tuple[list[CustomerListItemOut], int]:
    debt_sum = (
        select(func.coalesce(func.sum(Order.balance_due), 0))
        .where(Order.customer_id == Customer.id, debts.debt_condition())
        .correlate(Customer)
        .scalar_subquery()
    )
    orders_count = (
        select(func.count(Order.id)).where(Order.customer_id == Customer.id).correlate(Customer).scalar_subquery()
    )
    last_order = (
        select(func.max(Order.created_at)).where(Order.customer_id == Customer.id).correlate(Customer).scalar_subquery()
    )
    conds = []
    if search:
        pat = f"%{_escape_like(search.strip())}%"
        digits = normalize_phone(search)
        c_or = [Customer.name.ilike(pat, escape="\\")]
        if digits:
            c_or.append(Customer.phone_normalized.like(f"%{digits}%"))
        conds.append(or_(*c_or))
    if has_debt is True:
        conds.append(debt_sum > 0)
    elif has_debt is False:
        conds.append(debt_sum == 0)
    total = db.scalar(select(func.count()).select_from(Customer).where(*conds)) or 0
    rows = db.execute(
        select(Customer, orders_count.label("oc"), debt_sum.label("ds"), last_order.label("lo"))
        .where(*conds)
        .order_by(Customer.name, Customer.id)
        .offset(offset)
        .limit(limit)
    ).all()
    items = [
        CustomerListItemOut(
            id=c.id, name=c.name, phone=c.phone, is_verified=c.is_verified, orders_count=oc or 0,
            debt_total=ds or ZERO,
            last_order_at=lo, created_at=c.created_at,
        )
        for c, oc, ds, lo in rows
    ]
    return items, total


def detail(db: Session, customer_id: uuid.UUID) -> CustomerDetailOut:
    c = get(db, customer_id)
    debt_rows = debts.list_debt_rows(db, customer_id=c.id, offset=0, limit=500)[0]
    orders_rows, orders_total = orders_svc.list_orders(db, offset=0, limit=50, customer_id=c.id)
    payments = db.scalars(
        select(Payment)
        .join(Order, Order.id == Payment.order_id)
        .where(Order.customer_id == c.id)
        .order_by(Payment.created_at.desc())
        .limit(50)
    ).all()
    return CustomerDetailOut(
        id=c.id,
        name=c.name,
        phone=c.phone,
        is_verified=c.is_verified,
        notes=c.notes,
        created_at=c.created_at,
        updated_at=c.updated_at,
        orders_count=orders_total,
        debt_total=sum((d.balance_due for d in debt_rows), ZERO),
        debts=debt_rows,
        recent_orders=orders_rows,
        recent_payments=[PaymentOut.model_validate(p) for p in payments],
    )


def create(db: Session, data: CustomerCreate, actor: User, ip: str | None) -> Customer:
    norm = normalize_phone(data.phone)
    if _verified_phone_owner(db, norm):
        raise AppError(409, "phone_taken", "Ya existe un cliente con ese teléfono")
    c = Customer(name=data.name, phone=data.phone, phone_normalized=norm, notes=data.notes or None, is_verified=True)
    db.add(c)
    db.flush()
    audit.record(db, user=actor, action="customer_created", entity_type="customer", entity_id=c.id,
                 details={"name": c.name}, ip=ip)
    return c


def update(db: Session, customer_id: uuid.UUID, data: CustomerUpdate, actor: User, ip: str | None) -> Customer:
    c = db.scalar(select(Customer).where(Customer.id == customer_id).with_for_update())
    if c is None:
        raise not_found("customer_not_found", "Cliente no encontrado")
    changes: dict[str, object] = {}
    sent = data.model_fields_set
    if "name" in sent:
        if data.name is None:
            raise AppError(422, "validation_error", "name no puede ser null")
        if c.name != data.name:
            changes["name"] = {"from": c.name, "to": data.name}
            c.name = data.name
    if "phone" in sent:
        norm = normalize_phone(data.phone)
        if norm != c.phone_normalized and c.is_verified and _verified_phone_owner(db, norm, c.id):
            raise AppError(409, "phone_taken", "Ya existe un cliente con ese teléfono")
        changes["phone"] = {"from": c.phone, "to": data.phone}
        c.phone = data.phone
        c.phone_normalized = norm
    if "notes" in sent:
        c.notes = data.notes or None
    try:
        db.flush()
    except IntegrityError:
        raise AppError(409, "phone_taken", "Ya existe un cliente con ese teléfono")
    if changes:
        audit.record(db, user=actor, action="customer_updated", entity_type="customer", entity_id=c.id,
                     details=changes, ip=ip)
    return c

