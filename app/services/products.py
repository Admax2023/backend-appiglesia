from __future__ import annotations

import uuid

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.errors import AppError, not_found
from app.models import OrderItem, Product, User
from app.schemas.catalog import ProductCreate, ProductUpdate
from app.services import audit


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_products(
    db: Session, *, offset: int, limit: int, include_inactive: bool = True, search: str | None = None
) -> tuple[list[Product], int]:
    conds = []
    if not include_inactive:
        conds.append(Product.is_active.is_(True))
    if search:
        pat = f"%{_escape_like(search.strip())}%"
        conds.append(or_(Product.name.ilike(pat, escape="\\"), Product.description.ilike(pat, escape="\\")))
    total = db.scalar(select(func.count()).select_from(Product).where(*conds)) or 0
    rows = db.scalars(
        select(Product).where(*conds).order_by(Product.sort_order, Product.name).offset(offset).limit(limit)
    ).all()
    return list(rows), total


def public_menu(db: Session) -> list[Product]:
    return list(
        db.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.sort_order, Product.name)).all()
    )


def get(db: Session, product_id: uuid.UUID, *, lock: bool = False) -> Product:
    stmt = select(Product).where(Product.id == product_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    p = db.scalar(stmt)
    if p is None:
        raise not_found("product_not_found", "Producto no encontrado")
    return p


def create(db: Session, data: ProductCreate, actor: User, ip: str | None) -> Product:
    p = Product(**data.model_dump())
    db.add(p)
    db.flush()
    audit.record(
        db, user=actor, action="product_created", entity_type="product", entity_id=p.id,
        details={"name": p.name, "price": p.price}, ip=ip,
    )
    return p


def update(db: Session, product_id: uuid.UUID, data: ProductUpdate, actor: User, ip: str | None) -> Product:
    p = get(db, product_id, lock=True)
    changes: dict[str, object] = {}
    for field in data.model_fields_set:
        value = getattr(data, field)
        if value is None and field in ("name", "price", "is_available", "sort_order"):
            raise AppError(422, "validation_error", f"{field} no puede ser null")
        if field == "description" and value == "":
            value = None
        if getattr(p, field) != value:
            changes[field] = {"from": getattr(p, field), "to": value}
            setattr(p, field, value)
    db.flush()
    if changes:
        action = "product_price_changed" if "price" in changes else "product_updated"
        audit.record(db, user=actor, action=action, entity_type="product", entity_id=p.id, details=changes, ip=ip)
    return p


def set_active(db: Session, product_id: uuid.UUID, active: bool, actor: User, ip: str | None) -> Product:
    p = get(db, product_id, lock=True)
    if p.is_active != active:
        p.is_active = active
        db.flush()
        audit.record(
            db, user=actor, action="product_activated" if active else "product_deactivated",
            entity_type="product", entity_id=p.id, details={"name": p.name}, ip=ip,
        )
    return p


def delete(db: Session, product_id: uuid.UUID, actor: User, ip: str | None) -> None:
    p = get(db, product_id, lock=True)
    if db.scalar(select(exists().where(OrderItem.product_id == p.id))):
        raise AppError(409, "product_in_use", "El producto tiene pedidos asociados; desactívalo en lugar de eliminarlo")
    audit.record(db, user=actor, action="product_deleted", entity_type="product", entity_id=p.id,
                 details={"name": p.name, "price": p.price}, ip=ip)
    db.delete(p)
    db.flush()
