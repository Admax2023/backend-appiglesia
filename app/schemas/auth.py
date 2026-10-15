from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import StringConstraints

from app.enums import UserRole
from app.schemas.common import InModel, ORMModel


class LoginIn(InModel):
    username: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=False)]


class RefreshIn(InModel):
    refresh_token: Annotated[str, StringConstraints(min_length=10, max_length=255)]


class LogoutIn(InModel):
    refresh_token: Annotated[str, StringConstraints(min_length=10, max_length=255)]


class ChangePasswordIn(InModel):
    current_password: Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=False)]
    new_password: Annotated[str, StringConstraints(min_length=10, max_length=256, strip_whitespace=False)]


class UserOut(ORMModel):
    id: uuid.UUID
    username: str
    full_name: str
    role: UserRole
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class TokenOut(ORMModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    user: UserOut
