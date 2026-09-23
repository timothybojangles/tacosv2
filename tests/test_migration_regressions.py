"""Offline migration obligations; strict xfails identify known legacy defects.

Only assertion failures are expected. Import/setup/runtime errors must fail the
suite normally. Remove the xfail after implementing the correct behaviour.
All network access is prohibited by the fixture, even if a mock is missed.
"""
import csv
import json
import socket
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import validator
import sync_sales_orders as sales
from brightpearl import inventory_import as catalogue
from brightpearl.common import Credentials


@pytest.fixture(autouse=True)
def isolated_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def deny_network(*args, **kwargs):
        raise RuntimeError("Migration regression tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)


@pytest.fixture
def inventory_db(tmp_path):
    path = tmp_path / "inventory.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE product_catalogue
                (productId INTEGER, SKU TEXT, stockTracked INTEGER);
            INSERT INTO product_catalogue VALUES (101, 'TEST-001', 1);
            CREATE TABLE ref_warehouses (warehouseId INTEGER, name TEXT);
            INSERT INTO ref_warehouses VALUES (1, 'Main');
            INSERT INTO ref_warehouses VALUES (2, 'Other');
            CREATE TABLE ref_locations (
                locationId INTEGER, warehouseId INTEGER, zoneId INTEGER,
                groupingA TEXT, groupingB TEXT, groupingC TEXT, groupingD TEXT,
                barcode TEXT
            );
            INSERT INTO ref_locations VALUES (10, 1, NULL, 'A', NULL, NULL, NULL, NULL);
            INSERT INTO ref_locations VALUES (20, 2, NULL, 'B', NULL, NULL, NULL, NULL);
        """)
    return path


def test_named_inventory_location_resolves_in_selected_warehouse(inventory_db):
    with sqlite3.connect(inventory_db) as conn:
        assert validator._lookup_location_id(conn.cursor(), 1, "A") == 10
        assert validator._lookup_location_id(conn.cursor(), 2, "A") is None


@pytest.mark.parametrize("location", ["999", "20"])
def test_numeric_location_requires_existence_and_warehouse(inventory_db, location):
    with sqlite3.connect(inventory_db) as conn:
        assert validator._lookup_location_id(conn.cursor(), 1, location) is None


def run_inventory_validation(tmp_path, inventory_db, quantity, cost):
    source = tmp_path / "stock.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sku", "warehouseId", "locationId", "quantity", "costprice"])
        writer.writeheader()
        writer.writerow(dict(sku="TEST-001", warehouseId="1", locationId="10", quantity=quantity, costprice=cost))
    with patch.object(validator, "log"), patch.object(
        validator, "get_settings", return_value=SimpleNamespace(unmatched_output_dir=str(tmp_path / "errors"))
    ):
        return validator.validate_and_enrich_inventory(str(source), str(inventory_db), "synthetic")


def test_well_formed_inventory_row_is_accepted(tmp_path, inventory_db):
    assert run_inventory_validation(tmp_path, inventory_db, "3", "2.50") == 1


@pytest.mark.parametrize("quantity,cost", [("not-a-number", "2.50"), ("3", "not-a-number")])
def test_malformed_inventory_numbers_are_rejected(tmp_path, inventory_db, quantity, cost):
    assert run_inventory_validation(tmp_path, inventory_db, quantity, cost) == 0


def test_missing_inventory_fields_are_reported(tmp_path, inventory_db):
    source = tmp_path / "stock.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sku", "warehouseId", "locationName", "quantity", "costprice"],
        )
        writer.writeheader()
        writer.writerow(
            dict(sku="TEST-001", warehouseId="1", locationName="", quantity="3", costprice="2.50")
        )
    errors = tmp_path / "errors"
    with patch.object(validator, "log"), patch.object(
        validator, "get_settings", return_value=SimpleNamespace(unmatched_output_dir=str(errors))
    ):
        assert validator.validate_and_enrich_inventory(str(source), str(inventory_db), "synthetic") == 0

    with (errors / "synthetic_inventory_missing_required_row.csv").open(encoding="utf-8") as handle:
        rejected = list(csv.DictReader(handle))
    assert rejected[0]["validation_error"] == "missing required fields: locationName"


def test_inventory_row_reports_every_independent_validation_issue(tmp_path, inventory_db):
    source = tmp_path / "stock.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sku", "warehouseId", "locationId", "quantity", "costprice"],
        )
        writer.writeheader()
        writer.writerow(
            dict(sku="UNKNOWN-SKU", warehouseId="1", locationId="999", quantity="3", costprice="2.50")
        )
    errors = tmp_path / "errors"
    with patch.object(validator, "log"), patch.object(
        validator, "get_settings", return_value=SimpleNamespace(unmatched_output_dir=str(errors))
    ):
        assert validator.validate_and_enrich_inventory(str(source), str(inventory_db), "synthetic") == 0

    with (errors / "synthetic_inventory_rejected.csv").open(encoding="utf-8") as handle:
        rejected = list(csv.DictReader(handle))
    assert len(rejected) == 1
    assert rejected[0]["validation_categories"] == "unmatched_sku; unmatched_location"
    assert "SKU was not found" in rejected[0]["validation_error"]
    assert "Location was not found" in rejected[0]["validation_error"]


@pytest.mark.parametrize("quantity,cost,allowed,expected", [
    ("", "", True, 1),
    ("0", "0", True, 1),
    ("", "", False, 0),
    ("0", "0", False, 0),
])
def test_inventory_zero_and_blank_option(tmp_path, inventory_db, quantity, cost, allowed, expected):
    source = tmp_path / "stock.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sku", "warehouseId", "locationId", "quantity", "costprice"])
        writer.writeheader()
        writer.writerow(dict(sku="TEST-001", warehouseId="1", locationId="10", quantity=quantity, costprice=cost))
    with patch.object(validator, "log"), patch.object(
        validator, "get_settings", return_value=SimpleNamespace(unmatched_output_dir=str(tmp_path / "errors"))
    ):
        assert validator.validate_and_enrich_inventory(
            str(source), str(inventory_db), "synthetic", allow_zero_blanks=allowed
        ) == expected
    if expected:
        with sqlite3.connect(inventory_db) as conn:
            assert conn.execute("SELECT quantity, costprice FROM validated_inventory").fetchone() == (0.0, 0.0)


@pytest.mark.parametrize("price,allowed,expected", [
    ("12.50", False, 1),
    (0, True, 1),
    (0, False, 0),
    (None, True, 1),
    (None, False, 0),
    ("", True, 1),
    ("not-a-number", True, 0),
])
def test_inventory_cost_from_synced_price_list(tmp_path, inventory_db, price, allowed, expected):
    with sqlite3.connect(inventory_db) as conn:
        conn.execute("CREATE TABLE ref_price_lists (priceListId INTEGER, name TEXT, code TEXT)")
        conn.execute("INSERT INTO ref_price_lists VALUES (7, 'Cost', 'COST')")
        conn.execute("CREATE TABLE ref_price_list_values (productId INTEGER, priceListId INTEGER, value TEXT)")
        if price is not None:
            conn.execute("INSERT INTO ref_price_list_values VALUES (101, 7, ?)", (price,))
    source = tmp_path / "stock.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sku", "warehouseId", "locationId", "quantity"])
        writer.writeheader()
        writer.writerow(dict(sku="TEST-001", warehouseId="1", locationId="10", quantity="3"))
    with patch.object(validator, "log"), patch.object(
        validator, "get_settings", return_value=SimpleNamespace(unmatched_output_dir=str(tmp_path / "errors"))
    ):
        assert validator.validate_and_enrich_inventory(
            str(source), str(inventory_db), "synthetic", allow_zero_blanks=allowed, price_list_id=7
        ) == expected
    if expected:
        with sqlite3.connect(inventory_db) as conn:
            assert conn.execute("SELECT costprice FROM validated_inventory").fetchone()[0] == float(price or 0)
    else:
        with (tmp_path / "errors" / "synthetic_inventory_rejected.csv").open(encoding="utf-8") as handle:
            rejected = list(csv.DictReader(handle))[0]
        assert rejected["validation_categories"] == "missing_required"
        assert "price list" in rejected["validation_error"].lower()


def test_unmatched_inventory_sku_is_not_hidden_by_blank_price_option(tmp_path, inventory_db):
    with sqlite3.connect(inventory_db) as conn:
        conn.execute("CREATE TABLE ref_price_lists (priceListId INTEGER, name TEXT)")
        conn.execute("INSERT INTO ref_price_lists VALUES (7, 'Cost')")
    source = tmp_path / "stock.csv"
    source.write_text("sku,warehouseId,locationId,quantity\nUNKNOWN,1,10,3\n", encoding="utf-8")
    with patch.object(validator, "log"), patch.object(
        validator, "get_settings", return_value=SimpleNamespace(unmatched_output_dir=str(tmp_path / "errors"))
    ):
        assert validator.validate_and_enrich_inventory(
            str(source), str(inventory_db), "synthetic", allow_zero_blanks=True, price_list_id=7
        ) == 0
    with (tmp_path / "errors" / "synthetic_inventory_rejected.csv").open(encoding="utf-8") as handle:
        rejected = list(csv.DictReader(handle))[0]
    assert rejected["validation_categories"] == "unmatched_sku"


def test_saved_order_id_prevents_second_order_creation():
    order = dict(id=1, order_ref="TEST-ORDER", orderId=123, payment_amount="10",
                 payment_method_code="TEST", payment_date="2026-01-01")
    with (
        patch.object(sales, "ensure_account_binding"),
        patch.object(sales, "fetch_credentials", return_value=Credentials("test", "test", "euw1")),
        patch.object(sales, "load_validated_orders", return_value=[order]),
        patch.object(sales, "build_bp_order_payload", return_value={}),
        patch.object(sales, "post_order", return_value=(True, 456)) as create_order,
        patch.object(sales, "update_order_id"),
        patch.object(sales, "build_bp_payment_payload", return_value={}),
        patch.object(sales, "post_payment", return_value=(False, None)),
        patch.object(sales, "write_failed_orders_csv"),
        patch.object(sales, "_log"),
    ):
        sales.main("synthetic", "unused.db", progress_callback=lambda *args: None)
    create_order.assert_not_called()


def product_page(product_id, more=False):
    # Positional product-search fixture follows _flatten_product_rows.
    row = [product_id, "Test product", f"TEST-{product_id}", None, None, None,
           None, None, True, None, None, None, None, None, None, None, None, None]
    return json.dumps({"response": {"results": [row], "metaData": {
        "morePagesAvailable": more, "lastResult": 1, "resultsAvailable": 2 if more else 1
    }}}), 10, 0


def test_failed_reference_refresh_preserves_previous_catalogue(tmp_path):
    db = str(tmp_path / "catalogue.db")
    with (
        patch.object(catalogue, "fetch_credentials", return_value=Credentials("test", "test", "euw1")),
        patch.object(catalogue, "get_base_currency", return_value=None),
        patch.object(catalogue, "log"),
        patch.object(catalogue, "log_search_progress"),
        patch.object(catalogue, "record_api_update"),
        patch.object(catalogue, "should_pause_for_throttle", return_value=False),
        patch.object(catalogue, "get_upload_retry_settings", return_value=(1, 0)),
        patch.object(catalogue, "send_request", side_effect=[
            product_page(101), product_page(202, more=True), (None, 0, 0)
        ]),
    ):
        catalogue.update_product_catalogue("synthetic", db)
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT productId FROM product_catalogue").fetchall() == [(101,)]
        with pytest.raises(RuntimeError, match="previous complete catalogue was preserved"):
            catalogue.update_product_catalogue("synthetic", db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT productId FROM product_catalogue").fetchall() == [(101,)]
        assert conn.execute(
            "SELECT status, completed, total FROM reference_sync_progress WHERE reference_name='products'"
        ).fetchone() == ("failed", 1, 2)
        assert conn.execute("SELECT productId FROM product_catalogue_sync").fetchall() == [(202,)]
