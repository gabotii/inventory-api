"""Authorization policy: the single place that maps actions to minimum roles.

Services call ``authorize`` before doing any work, so every entry point (HTTP,
CLI, future workers) is subject to the same rules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import PermissionDeniedError
from app.models.enums import Role
from app.models.user import User


class Permission(StrEnum):
    READ_INVENTORY = "read_inventory"
    RECORD_MOVEMENT = "record_movement"
    ADJUST_STOCK = "adjust_stock"
    MANAGE_CATALOG = "manage_catalog"
    MANAGE_USERS = "manage_users"
    READ_AUDIT = "read_audit"


POLICY: dict[Permission, Role] = {
    Permission.READ_INVENTORY: Role.VIEWER,
    Permission.RECORD_MOVEMENT: Role.OPERATOR,
    Permission.ADJUST_STOCK: Role.MANAGER,
    Permission.MANAGE_CATALOG: Role.MANAGER,
    Permission.MANAGE_USERS: Role.ADMIN,
    Permission.READ_AUDIT: Role.ADMIN,
}


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ip_address: str | None = None


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated user on whose behalf a service call runs."""

    user: User
    meta: RequestMeta

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> Role:
        return self.user.role_enum


def is_allowed(role: Role, permission: Permission) -> bool:
    return role.at_least(POLICY[permission])


def authorize(actor: Actor, permission: Permission) -> None:
    if not actor.user.is_active or not is_allowed(actor.role, permission):
        raise PermissionDeniedError(details={"required_role": POLICY[permission].value})
