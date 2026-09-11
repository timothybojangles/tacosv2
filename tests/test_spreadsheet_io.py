import csv
from zipfile import ZipFile

import pytest

from csv_safety import check_csv_encoding, open_csv, open_table, repair_csv_encoding


def test_xlsx_round_trip_with_dict_reader_and_writer(tmp_path):
    path = tmp_path / "products.xlsx"
    with open_table(path) as handle:
        writer = csv.DictWriter(handle, fieldnames=["sku", "description", "quantity"])
        writer.writeheader()
        writer.writerow({"sku": "A-1", "description": "Taco & salsa <hot>", "quantity": 12})
        writer.writerow({"sku": "A-2", "description": "multiline\nvalue", "quantity": 0})

    assert path.read_bytes().startswith(b"PK")
    with ZipFile(path) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()
    with open_csv(path) as handle:
        assert list(csv.DictReader(handle)) == [
            {"sku": "A-1", "description": "Taco & salsa <hot>", "quantity": "12"},
            {"sku": "A-2", "description": "multiline\nvalue", "quantity": "0"},
        ]


def test_csv_remains_supported(tmp_path):
    path = tmp_path / "products.csv"
    with open_table(path) as handle:
        csv.writer(handle).writerows([["sku", "name"], ["1", "Taco"]])
    with open_csv(path) as handle:
        assert list(csv.reader(handle)) == [["sku", "name"], ["1", "Taco"]]


def test_encoding_utilities_ignore_binary_xlsx(tmp_path):
    path = tmp_path / "book.xlsx"
    with open_table(path) as handle:
        csv.writer(handle).writerow(["header"])
    check_csv_encoding(path)
    with pytest.raises(Exception):
        repair_csv_encoding(path)
