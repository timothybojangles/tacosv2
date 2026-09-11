import csv
from pathlib import Path

import pytest

from csv_safety import CSVEncodingError, check_csv_encoding, open_csv, repair_csv_encoding


def test_error_identifies_file_line_column_bytes_and_data(tmp_path):
    path = tmp_path / "contacts.csv"
    path.write_bytes(b"name,email\nJo\x96anna,user@example.com\n")

    with pytest.raises(CSVEncodingError) as caught:
        check_csv_encoding(str(path))

    message = str(caught.value)
    assert "contacts.csv" in message
    assert "line 2, column 3" in message
    assert "96" in message
    assert "Affected data" in message
    assert "automatic repair" in message


def test_repair_preserves_original_and_replaces_bad_data(tmp_path):
    path = tmp_path / "contacts.csv"
    original = b"name\nJo\x96anna\n"
    path.write_bytes(original)

    repaired = Path(repair_csv_encoding(str(path)))

    assert path.read_bytes() == original
    assert repaired.name == "contacts.utf8-fixed.csv"
    assert repaired.read_text(encoding="utf-8") == "name\nJo?anna\n"
    with open_csv(str(repaired)) as handle:
        assert list(csv.DictReader(handle)) == [{"name": "Jo?anna"}]


def test_valid_utf8_non_ascii_data_is_not_changed(tmp_path):
    path = tmp_path / "contacts.csv"
    path.write_text("name\nMichał\n", encoding="utf-8-sig")

    check_csv_encoding(str(path))
    with open_csv(str(path)) as handle:
        assert list(csv.DictReader(handle)) == [{"name": "Michał"}]
