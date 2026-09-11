from row_validation import (
    PRODUCT_NAME_MAX_LENGTH,
    SKU_MAX_LENGTH,
    product_field_length_errors,
    row_with_validation_error,
)


def test_product_fields_accept_values_at_character_limits():
    assert product_field_length_errors(
        "S" * SKU_MAX_LENGTH,
        "N" * PRODUCT_NAME_MAX_LENGTH,
    ) == []


def test_product_fields_report_each_overlong_value():
    errors = product_field_length_errors(
        "S" * (SKU_MAX_LENGTH + 1),
        "N" * (PRODUCT_NAME_MAX_LENGTH + 1),
    )

    assert errors == [
        "SKU exceeds 32 characters",
        "Product name exceeds 128 characters",
    ]


def test_exception_row_preserves_source_and_adds_detail():
    source = {"sku": "too-long"}

    result = row_with_validation_error(source, ["first", "second"])

    assert result == {"sku": "too-long", "validation_error": "first; second"}
    assert "validation_error" not in source
