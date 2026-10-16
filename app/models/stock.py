from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import MovementType, sql_in


class StockLevel(Base):
    """Current on-hand quantity per (product, warehouse).

    Derived from the movement ledger and only ever changed in the same
    transaction that appends a movement. Rows appear on the first movement.
    """

    __tablename__ = "stock_levels"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        Index("ix_stock_levels_warehouse_id", "warehouse_id"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True
    )
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


_SIGN_RULES = " OR ".join(
    [
        "(movement_type = 'receipt' AND quantity > 0)",
        "(movement_type = 'issue' AND quantity < 0)",
        "(movement_type = 'transfer_in' AND quantity > 0)",
        "(movement_type = 'transfer_out' AND quantity < 0)",
        "(movement_type = 'adjustment' AND quantity <> 0)",
    ]
)


class StockMovement(Base):
    """Append-only ledger entry. Never updated or deleted by the application."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint(f"movement_type IN ({sql_in(MovementType)})", name="type_valid"),
        CheckConstraint(_SIGN_RULES, name="quantity_sign_matches_type"),
        CheckConstraint("balance_after >= 0", name="balance_after_non_negative"),
        CheckConstraint(
            "(transfer_id IS NOT NULL) = (movement_type IN ('transfer_in', 'transfer_out'))",
            name="transfer_id_iff_transfer",
        ),
        Index("ix_stock_movements_product_id_id", "product_id", "id"),
        Index("ix_stock_movements_warehouse_id_id", "warehouse_id", "id"),
        Index("ix_stock_movements_transfer_id", "transfer_id"),
        Index("ix_stock_movements_created_at", "created_at"),
        Index("ix_stock_movements_created_by", "created_by"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    movement_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    transfer_id: Mapped[uuid.UUID | None] = mapped_column()
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
