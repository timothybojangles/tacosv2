"""Shared validation for product identifiers used by CSV imports."""

SKU_MAX_LENGTH = 32
PRODUCT_NAME_MAX_LENGTH = 128


def product_field_length_errors(sku: str = "", product_name: str = "") -> list[str]:
    """Return human-readable errors for overlong SKU and product-name values."""
    errors = []
    if len(sku or "") > SKU_MAX_LENGTH:
        errors.append(f"SKU exceeds {SKU_MAX_LENGTH} characters")
    if len(product_name or "") > PRODUCT_NAME_MAX_LENGTH:
        errors.append(f"Product name exceeds {PRODUCT_NAME_MAX_LENGTH} characters")
    return errors


def row_with_validation_error(row: dict, errors: list[str]) -> dict:
    """Copy an input row and include details suitable for exception CSVs."""
    result = dict(row)
    result["validation_error"] = "; ".join(errors)
    return result
