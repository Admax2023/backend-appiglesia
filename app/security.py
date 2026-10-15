from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

_ph = PasswordHasher()  # argon2id with library defaults
_DUMMY_HASH = _ph.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time: verifies against a dummy hash when the user does not exist."""
    try:
        ok = _ph.verify(password_hash or _DUMMY_HASH, password)
        return bool(ok) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def new_tracking_token() -> str:
    return secrets.token_urlsafe(24)


def create_access_token(user_id: uuid.UUID, role: str) -> tuple[str, int]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    expires = timedelta(minutes=s.access_token_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + expires).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm), int(expires.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    s = get_settings()
    payload = jwt.decode(
        token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub", "type"]}
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload
