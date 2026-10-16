from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    MANAGER = "manager"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank


_ROLE_RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.MANAGER: 2, Role.ADMIN: 3}


class MovementType(StrEnum):
    RECEIPT = "receipt"
    ISSUE = "issue"
    ADJUSTMENT = "adjustment"
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"


def sql_in(values: type[StrEnum]) -> str:
    """Render enum values for a CHECK constraint: ``'a', 'b', 'c'``."""
    return ", ".join(f"'{member.value}'" for member in values)
