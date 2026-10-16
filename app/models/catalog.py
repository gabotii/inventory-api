from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

SKU_PATTERN = r"^[A-Z0-9][A-Z0-9._\-]{0,63}$"
WAREHOUSE_CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_\-]{0,31}$"


class Category(TimestampMixin, Base):
    __tablename__ = "categories"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": version, "eager_defaults": True}  # noqa: RUF012


# Case-insensitive uniqueness ("Tools" and "tools" are the same category).
Index("uq_categories_name_lower", func.lower(Category.name), unique=True)


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(f"sku ~ '{SKU_PATTERN}'", name="sku_format"),
        CheckConstraint("reorder_level >= 0", name="reorder_level_non_negative"),
        Index("ix_products_category_id", "category_id"),
        Index(
            "ix_products_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_products_sku_trgm",
            "sku",
            postgresql_using="gin",
            postgresql_ops={"sku": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="unit")
    reorder_level: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": version, "eager_defaults": True}  # noqa: RUF012


class Warehouse(TimestampMixin, Base):
    __tablename__ = "warehouses"
    __table_args__ = (CheckConstraint(f"code ~ '{WAREHOUSE_CODE_PATTERN}'", name="code_format"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    __mapper_args__ = {"version_id_col": version, "eager_defaults": True}  # noqa: RUF012
