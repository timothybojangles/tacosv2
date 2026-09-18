import csv
import json
import sqlite3

from brightpearl.common import Credentials
from brightpearl import product_catalogue_export as catalogue
from csv_safety import open_csv


def _product_payload():
    return {
        "id": 101,
        "brandId": 11,
        "collectionId": 12,
        "productTypeId": 13,
        "productGroupId": 14,
        "nominalCodeStock": "1000",
        "nominalCodePurchases": "2000",
        "nominalCodeSales": "3000",
        "identity": {
            "sku": "MS",
            "barcode": "fcMs01",
            "ean": "EAN",
            "upc": "UPC",
            "isbn": "ISBN",
            "mpn": "MPN",
        },
        "featured": True,
        "stock": {
            "stockTracked": True,
            "weight": {"magnitude": 1.5},
            "dimensions": {"length": 2, "height": 3, "width": 4, "volume": 24},
        },
        "financialDetails": {"taxable": True, "taxCode": {"id": 7, "code": "T20"}},
        "composition": {
            "bundle": True,
            "bundleComponents": [{"productId": 102, "productQuantity": 2}],
        },
        "reporting": {"categoryId": 21, "subcategoryId": 22, "seasonId": 23},
        "primarySupplierId": 31,
        "status": "LIVE",
        "salesPopupMessage": "Message",
        "createdOn": "2025-01-01T00:00:00Z",
        "updatedOn": "2025-01-02T00:00:00Z",
        "version": 4,
        "salesChannels": [{
            "salesChannelName": "Brightpearl",
            "productName": "Product",
            "productCondition": "new",
            "description": {"languageCode": "en", "text": "Description"},
            "shortDescription": {"languageCode": "en", "text": "Short"},
            "categories": [{"categoryCode": "21"}],
        }],
        "variations": [{"optionId": 41, "optionValueId": 42}],
        "seasonIds": [23],
        "customFields": {"PCF_COLOR": {"id": 51, "value": "Red"}},
        "nullCustomFields": ["PCF_EMPTY"],
        "warehouses": {"61": {"defaultLocationId": 62, "reorderLevel": 3, "reorderQuantity": 5}},
    }


def test_sync_persists_complete_identity_and_raw_product(monkeypatch, tmp_path):
    db_path = tmp_path / "catalogue.db"
    product = _product_payload()
    monkeypatch.setattr(catalogue, "ensure_account_binding", lambda *_: None)
    monkeypatch.setattr(
        catalogue, "fetch_credentials", lambda *_: Credentials("app", "token", "euw1")
    )
    monkeypatch.setattr(catalogue, "sync_product_reference_data", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(catalogue, "_fetch_product_uris", lambda *_args, **_kwargs: ["/product"])
    monkeypatch.setattr(
        catalogue,
        "send_request",
        lambda *_args, **_kwargs: (json.dumps({"response": [product]}), 0, 100),
    )
    monkeypatch.setattr(catalogue, "record_api_update", lambda *_: None)

    assert catalogue.sync_product_catalogue_export_data("account", str(db_path)) == 1

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        saved = conn.execute("SELECT * FROM export_products WHERE productId = 101").fetchone()
        assert {key: saved[key] for key in ("sku", "barcode", "ean", "upc", "isbn", "mpn")} == {
            "sku": "MS", "barcode": "fcMs01", "ean": "EAN", "upc": "UPC",
            "isbn": "ISBN", "mpn": "MPN",
        }
        assert json.loads(saved["raw_json"]) == product
        assert conn.execute("SELECT COUNT(*) FROM export_product_sales_channels").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM export_product_categories").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM export_product_variations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM export_product_seasons").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM export_product_bundle_components").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM export_product_custom_fields").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM export_product_warehouses").fetchone()[0] == 1


def test_existing_database_is_migrated_and_identity_is_exported_to_xlsx(tmp_path):
    old_db_path = tmp_path / "old-catalogue.db"
    with sqlite3.connect(old_db_path) as conn:
        conn.execute("CREATE TABLE export_products (productId INTEGER PRIMARY KEY, sku TEXT, barcode TEXT)")
        catalogue._ensure_export_tables(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(export_products)")}
        assert {"ean", "upc", "isbn", "mpn"} <= columns

    db_path = tmp_path / "catalogue.db"
    with sqlite3.connect(db_path) as conn:
        catalogue._ensure_export_tables(conn)
        conn.execute(
            "INSERT INTO export_products (productId, sku, barcode, ean, upc, isbn, mpn) "
            "VALUES (101, 'MS', 'fcMs01', 'EAN', 'UPC', 'ISBN', 'MPN')"
        )

    output_path = tmp_path / "catalogue.xlsx"
    assert catalogue.export_synced_product_catalogue_to_csv(str(db_path), str(output_path)) == 1

    with open_csv(output_path) as handle:
        row = next(csv.DictReader(handle))
    assert {key: row[key] for key in ("sku", "barcode", "ean", "upc", "isbn", "mpn")} == {
        "sku": "MS", "barcode": "fcMs01", "ean": "EAN", "upc": "UPC",
        "isbn": "ISBN", "mpn": "MPN",
    }
