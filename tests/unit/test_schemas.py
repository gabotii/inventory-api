from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.catalog import ProductCreate, ProductUpdate, WarehouseCreate
from app.schemas.stock import MovementCreate, TransferCreate
from app.schemas.users import UserCreate, UserUpdate


def test_sku_is_trimmed_and_uppercased() -> None:
    product = ProductCreate(sku="  ab-12.x ", name=" Bolt ", unit=" KG ")
    assert product.sku == "AB-12.X"
    assert product.name == "Bolt"
    assert product.unit == "kg"


@pytest.mark.parametrize("sku", ["", "ab cd", "-AB", "AB/1", "Ñ1", "X" * 65, "a'b"])
def test_invalid_skus_rejected(sku: str) -> None:
    with pytest.raises(ValidationError):
        ProductCreate(sku=sku, name="x")


def test_warehouse_code_normalised() -> None:
    assert WarehouseCreate(code=" mad-1 ", name="Madrid").code == "MAD-1"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ProductCreate.model_validate({"sku": "A", "name": "x", "is_admin": True})


def test_email_lowercased_and_password_policy_enforced() -> None:
    user = UserCreate(email="Ana@Example.COM", full_name="Ana", password="long-enough-pass")
    assert user.email == "ana@example.com"
    with pytest.raises(ValidationError):
        UserCreate(email="ana@example.com", full_name="Ana", password="short")
    with pytest.raises(ValidationError):
        UserCreate(email="ana@example.com", full_name="Ana", password="x" * 129)
    with pytest.raises(ValidationError):
        UserCreate(email="not-an-email", full_name="Ana", password="long-enough-pass")


def test_patch_tracks_only_sent_fields() -> None:
    patch = ProductUpdate.model_validate({"version": 3, "description": None})
    assert patch.changes() == {"description": None}


@pytest.mark.parametrize("field", ["name", "unit", "reorder_level", "is_active"])
def test_patch_rejects_null_for_non_nullable_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="cannot be null"):
        ProductUpdate.model_validate({"version": 1, field: None})


def test_patch_requires_version() -> None:
    with pytest.raises(ValidationError):
        UserUpdate.model_validate({"full_name": "x"})


def _movement(movement_type: str, quantity: int) -> MovementCreate:
    return MovementCreate(
        product_id=uuid.uuid4(),
        warehouse_id=uuid.uuid4(),
        movement_type=movement_type,  # type: ignore[arg-type]
        quantity=quantity,
    )


def test_issue_quantity_is_stored_negative() -> None:
    assert _movement("issue", 5).signed_quantity == -5
    assert _movement("receipt", 5).signed_quantity == 5
    assert _movement("adjustment", -2).signed_quantity == -2


@pytest.mark.parametrize(
    ("movement_type", "quantity"),
    [("receipt", 0), ("receipt", -1), ("issue", 0), ("issue", -3), ("adjustment", 0)],
)
def test_invalid_movement_quantities(movement_type: str, quantity: int) -> None:
    with pytest.raises(ValidationError):
        _movement(movement_type, quantity)


def test_movement_quantity_upper_bound() -> None:
    with pytest.raises(ValidationError):
        _movement("receipt", 1_000_000_001)


def test_transfer_types_cannot_be_posted_directly() -> None:
    with pytest.raises(ValidationError):
        _movement("transfer_in", 5)


def test_transfer_requires_distinct_warehouses() -> None:
    warehouse = uuid.uuid4()
    with pytest.raises(ValidationError, match="must differ"):
        TransferCreate(
            product_id=uuid.uuid4(),
            from_warehouse_id=warehouse,
            to_warehouse_id=warehouse,
            quantity=1,
        )
