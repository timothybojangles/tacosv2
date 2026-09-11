from brightpearl.settings import AppSettings, normalize_settings


def test_stock_correction_batch_size_defaults_to_fifty():
    assert normalize_settings({}).stock_correction_batch_size == 50


def test_stock_correction_batch_size_accepts_bounds_and_values_between_them():
    for value in (1, 237, 500):
        assert normalize_settings({"stock_correction_batch_size": str(value)}).stock_correction_batch_size == value


def test_stock_correction_batch_size_rejects_values_outside_bounds():
    default = AppSettings().stock_correction_batch_size
    for value in (0, 501, -1, "not a number"):
        assert normalize_settings({"stock_correction_batch_size": value}).stock_correction_batch_size == default
