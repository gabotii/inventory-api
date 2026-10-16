from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, ClassVar

from pydantic import AfterValidator, EmailStr, Field, StringConstraints

from app.models.enums import Role
from app.schemas.common import ApiModel, Name, PatchModel, ResponseModel

MIN_PASSWORD_LENGTH = 12
# Argon2 handles long inputs fine, but an upper bound stops multi-megabyte
# "passwords" from being used to burn CPU.
MAX_PASSWORD_LENGTH = 128


def _lower(value: str) -> str:
    return value.lower()


# Whole address lower-cased: we treat emails as case-insensitive identifiers.
Email = Annotated[EmailStr, Field(max_length=320), AfterValidator(_lower)]
Password = Annotated[
    str,
    StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH),
]
# Existing passwords are never validated against the policy (it may change);
# only the length cap applies.
SubmittedPassword = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PASSWORD_LENGTH)]


class UserCreate(ApiModel):
    email: Email
    full_name: Name
    role: Role = Role.VIEWER
    password: Password


class UserUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset({"full_name", "role", "is_active"})

    full_name: Name | None = None
    role: Role | None = None
    is_active: bool | None = None


class PasswordReset(ApiModel):
    new_password: Password


class PasswordChange(ApiModel):
    current_password: SubmittedPassword
    new_password: Password


class UserRead(ResponseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
