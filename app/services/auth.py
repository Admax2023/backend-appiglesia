"""Login, refresh-token rotation with reuse detection, logout."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import UserRole
from app.errors import AppError
from app.models import RefreshToken, User
from app.schemas.auth import TokenOut, UserOut
from app.security import (
    create_access_token,
    hash_password,
    new_opaque_token,
    password_needs_rehash,
    sha256_hex,
    verify_password,
)
from app.services import audit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _invalid_refresh(code: str = "invalid_refresh_token", detail: str = "Sesión inválida") -> AppError:
    return AppError(401, code, detail, headers={"WWW-Authenticate": "Bearer"})


def _issue(db: Session, user: User, family_id: uuid.UUID, ua: str | None, ip: str | None) -> tuple[str, RefreshToken]:
    raw = new_opaque_token()
    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        family_id=family_id,
        token_hash=sha256_hex(raw),
        expires_at=_now() + timedelta(days=get_settings().refresh_token_days),
        user_agent=(ua or "")[:255] or None,
        ip_address=ip,
    )
    db.add(row)
    db.flush()
    return raw, row


def _token_out(user: User, refresh_raw: str) -> TokenOut:
    access, expires_in = create_access_token(user.id, user.role.value)
    return TokenOut(
        access_token=access,
        expires_in=expires_in,
        refresh_token=refresh_raw,
        refresh_expires_in=get_settings().refresh_token_days * 86400,
        user=UserOut.model_validate(user),
    )


def login(db: Session, username: str, password: str, ua: str | None, ip: str | None) -> TokenOut:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    ok = verify_password(user.password_hash if user else None, password)
    if not user or not ok or not user.is_active:
        raise AppError(401, "invalid_credentials", "Usuario o contraseña incorrectos")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = _now()
    raw, _ = _issue(db, user, uuid.uuid4(), ua, ip)
    audit.record(db, user=user, action="login", entity_type="user", entity_id=user.id, ip=ip)
    return _token_out(user, raw)


def _revoke_family(db: Session, family_id: uuid.UUID) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


def refresh(db: Session, raw: str, ua: str | None, ip: str | None) -> TokenOut:
    row = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == sha256_hex(raw))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise _invalid_refresh()
    if row.used_at is not None or row.revoked_at is not None:
        # Reuse of a rotated/revoked token => possible theft: revoke the whole family.
        was_rotated = row.used_at is not None
        _revoke_family(db, row.family_id)
        if was_rotated:
            audit.record(
                db,
                user=None,
                action="refresh_token_reuse_detected",
                entity_type="user",
                entity_id=row.user_id,
                details={"family_id": row.family_id},
                ip=ip,
            )
        db.commit()
        raise _invalid_refresh("refresh_token_reused" if was_rotated else "refresh_token_revoked",
                               "La sesión fue revocada. Inicia sesión de nuevo.")
    if row.expires_at <= _now():
        raise _invalid_refresh("refresh_token_expired", "La sesión expiró. Inicia sesión de nuevo.")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active or user.role != UserRole.admin:
        raise _invalid_refresh()
    new_raw, new_row = _issue(db, user, row.family_id, ua, ip)
    row.used_at = _now()
    row.replaced_by_id = new_row.id
    db.flush()
    return _token_out(user, new_raw)


def logout(db: Session, raw: str) -> None:
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw)))
    if row is not None:
        _revoke_family(db, row.family_id)


def change_password(db: Session, user: User, current: str, new: str, ip: str | None) -> None:
    if not verify_password(user.password_hash, current):
        raise AppError(400, "invalid_credentials", "La contraseña actual no es correcta")
    user.password_hash = hash_password(new)
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    audit.record(db, user=user, action="password_changed", entity_type="user", entity_id=user.id, ip=ip)


def reset_password(db: Session, username: str, new: str) -> User:
    """Server-side reset (CLI only): sets a new password, reactivates the user and closes every session."""
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        raise AppError(404, "user_not_found", "El usuario no existe")
    if len(new) < 10:
        raise AppError(422, "weak_password", "La contraseña debe tener al menos 10 caracteres")
    user.password_hash = hash_password(new)
    user.is_active = True
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    audit.record(db, user=None, action="password_reset", entity_type="user", entity_id=user.id,
                 details={"username": user.username, "via": "cli"})
    return user


def create_user(db: Session, username: str, full_name: str, password: str) -> User:
    username = username.strip().lower()
    if len(password) < 10:
        raise AppError(422, "weak_password", "La contraseña debe tener al menos 10 caracteres")
    if db.scalar(select(User).where(User.username == username)):
        raise AppError(409, "username_taken", "El usuario ya existe")
    user = User(username=username, full_name=full_name.strip(), password_hash=hash_password(password), role=UserRole.admin)
    db.add(user)
    db.flush()
    return user
