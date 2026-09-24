import csv
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brightpearl.common import log
from tacos_engine import worker
from tacos_engine.worker import app_store, handle


def _request(method, params=None, request_id="request-1"):
    return {"protocolVersion": 1, "id": request_id, "method": method, "params": params or {}}


def test_logger_does_not_fail_on_windows_charmap_only_output(tmp_path, monkeypatch):
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    seen = []

    class CharmapOnly:
        def write(self, value):
            value.encode("cp1252")

        def flush(self):
            return None

    def callback(value):
        value.encode("cp1252")
        seen.append(value)

    monkeypatch.setattr(sys, "stdout", CharmapOnly())
    log("Status -> ok \u2192 unicode fallback", callback, log_file=None)
    assert seen == ["Status -> ok ? unicode fallback"]


def test_source_entrypoint_starts_outside_repository_root(tmp_path):
    entrypoint = Path(worker.__file__).resolve().parents[1] / "worker.py"
    env = os.environ.copy()
    env["TACOS_CREDENTIAL_BACKEND"] = "sqlite_plaintext"
    env["TACOS_DESKTOP_DATA_DIR"] = str(tmp_path / "appdata")
    completed = subprocess.run(
        [sys.executable, str(entrypoint)],
        cwd=entrypoint.parent,
        env=env,
        input=json.dumps(_request("shutdown", request_id="shutdown-source")) + "\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    messages = [json.loads(line) for line in completed.stdout.splitlines()]
    assert messages[0]["type"] == "ready"
    assert messages[-1]["result"] == {"shutdown": True}


def test_account_index_recovers_from_local_compatibility_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    root = tmp_path / "appdata"
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(root))
    (root / "db").mkdir(parents=True)
    with sqlite3.connect(root / "db" / "credentials.db") as conn:
        conn.execute(
            "CREATE TABLE credentials (account_name TEXT, app_ref TEXT, token TEXT, region TEXT, base_currency TEXT)"
        )
        conn.execute(
            "INSERT INTO credentials VALUES ('recovered', 'app', 'secret', 'euw1', 'GBP')"
        )

    store = app_store()
    with sqlite3.connect(store.ledger) as conn:
        assert conn.execute(
            "SELECT account_name, region, base_currency FROM accounts"
        ).fetchall() == [("recovered", "euw1", "GBP")]


def test_worker_imports_and_pages_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    source = tmp_path / "inventory.csv"
    source.write_text("sku,location,quantity\nA,MAIN,2\nB,BULK,5\n", encoding="utf-8")
    store = app_store()

    handle(
        store,
        {
            "protocolVersion": 1,
            "id": "job-1",
            "method": "importSyntheticCsv",
            "params": {"path": str(source)},
        },
    )
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    response = [line for line in lines if line.get("type") == "response"][-1]
    assert response["ok"] is True
    assert response["result"]["rowsRead"] == 2

    handle(
        store,
        {
            "protocolVersion": 1,
            "id": "preview-1",
            "method": "previewDataset",
            "params": {"datasetId": response["result"]["datasetId"], "filter": "bulk"},
        },
    )
    preview = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert preview["result"]["totalRows"] == 1
    assert preview["result"]["rows"][0]["sku"] == "B"


def test_account_credentials_are_saved_without_mixing_data_dbs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()

    handle(
        store,
        _request(
            "saveAccount",
            {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        ),
    )
    response = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert response["ok"] is True
    assert response["result"]["account"]["accountName"] == "demo"
    assert response["result"]["account"]["credentialStatus"] == "saved"

    db_path = Path(response["result"]["account"]["dataDbPath"])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT account_name FROM account_binding").fetchone() == ("demo",)

    try:
        worker.account_data_db(store, "other")
    except worker.WorkerError:
        raise AssertionError("Different accounts should receive different data DB paths")


def test_remove_account_deletes_credentials_and_local_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    for account in ("remove-me", "keep-me"):
        handle(store, _request("saveAccount", {
            "accountName": account, "appRef": "app", "token": "secret", "region": "euw1"
        }, f"save-{account}"))
        capsys.readouterr()
    removed_db = worker.account_data_db(store, "remove-me")

    handle(store, _request("removeAccount", {
        "accountName": "remove-me", "confirmAccountName": "wrong"
    }, "remove-wrong"))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "confirmation_required"

    handle(store, _request("removeAccount", {
        "accountName": "remove-me", "confirmAccountName": "remove-me"
    }, "remove-confirmed"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert result == {"removed": "remove-me", "dataRemoved": True}
    assert not removed_db.exists()
    assert worker.read_credential("remove-me") is None
    assert worker.read_credential("keep-me") is not None
    handle(store, _request("listAccounts", request_id="accounts-after-remove"))
    accounts = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]["accounts"]
    assert [account["accountName"] for account in accounts] == ["keep-me"]


def test_validate_account_uses_configuration_check_and_saves_base_currency(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(
        store,
        _request(
            "saveAccount",
            {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
            "save-1",
        ),
    )
    capsys.readouterr()

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"response": {"configuration": {"baseCurrencyCode": "GBP"}}},
    )
    with patch.object(worker.requests, "get", return_value=response) as get:
        handle(store, _request("validateAccount", {"accountName": "demo"}, "validate-1"))

    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["ok"] is True
    assert result["result"]["baseCurrency"] == "GBP"
    assert get.call_args.args[0].endswith("/integration-service/account-configuration")


def test_inventory_reference_sync_calls_inventory_specific_legacy_steps(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(
        store,
        _request(
            "saveAccount",
            {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
            "save-1",
        ),
    )
    capsys.readouterr()

    with (
        patch.object(
            worker,
            "update_product_catalogue",
            side_effect=lambda *args, **kwargs: (
                print("✅ products"),
                kwargs["log_callback"]("✅ products"),
                2,
            )[2],
        ) as products,
        patch.object(worker, "fetch_and_store_reference_tables", return_value={"warehouses": 1}) as refs,
        patch.object(worker, "update_location_catalogue", return_value=3) as locations,
        patch.object(worker, "sync_inventory_pricelists", return_value={"price_lists": 4, "price_list_values": 5}) as prices,
    ):
        handle(store, _request("syncInventoryReferences", {"accountName": "demo"}, "sync-1"))

    captured = capsys.readouterr().out
    assert "✅ products" not in captured
    assert "\\u2705 products" in captured
    result = json.loads(captured.splitlines()[-1])
    assert result["ok"] is True
    assert result["result"]["logs"] == ["✅ products"]
    assert result["result"]["results"] == {
        "products": 2,
        "warehouses": 1,
        "locations": 3,
        "priceLists": 4,
        "priceListValues": 5,
    }
    products.assert_called_once()
    refs.assert_called_once()
    locations.assert_called_once()
    prices.assert_called_once()


def test_go_live_operations_expose_legacy_scope(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    handle(app_store(), _request("legacyOperations", request_id="legacy-ops"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    operation_ids = [operation["id"] for operation in result["operations"]]
    assert operation_ids[:4] == ["inventory_import", "open_sales", "open_purchases", "historic_sales"]
    assert {"Go Live", "Configuration", "Maintenance", "Export", "API Tools", "Training"} <= set(result["categories"])
    assert result["operations"][1]["legacyModules"] == ["validator_sales_orders.py", "sync_sales_orders.py"]


def test_app_settings_roundtrip_uses_legacy_normalization(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request("saveAppSettings", {
        "settings": {"log_level": "debug", "stock_correction_batch_size": "501", "upload_max_retries": "7"}
    }, "save-settings"))
    saved = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]["settings"]
    assert saved["log_level"] == "DEBUG"
    assert saved["stock_correction_batch_size"] == 50
    assert saved["upload_max_retries"] == 7

    handle(store, _request("appSettings", request_id="settings"))
    loaded = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert loaded["settings"]["log_level"] == "DEBUG"
    assert loaded["options"]["stockCorrectionBatchSize"] == {"min": 1, "max": 500}


def test_logs_and_environment_use_app_data_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    log_dir = tmp_path / "appdata" / "output" / "debug"
    log_dir.mkdir(parents=True)
    (log_dir / "sync_debug.log").write_text("first\nsecond\n", encoding="utf-8")

    handle(store, _request("appLogs", request_id="logs"))
    logs = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]["logs"]
    sync_log = next(log for log in logs if log["id"] == "sync")
    assert sync_log["exists"] is True
    assert sync_log["path"].endswith("sync_debug.log")

    handle(store, _request("readAppLog", {"id": "sync", "maxChars": 20}, "read-log"))
    content = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]["content"]
    assert content == "first\nsecond\n"

    handle(store, _request("appEnvironment", request_id="environment"))
    environment = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert environment["dataRoot"].endswith("appdata")
    assert environment["legacyVersion"] == worker.LEGACY_APP_VERSION


def test_inventory_reference_sync_reports_persisted_product_progress(tmp_path, monkeypatch, capfd):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request("saveAccount", {
        "accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"
    }, "save-1"))
    capfd.readouterr()
    db_path = worker.account_data_db(store, "demo")

    def product_sync(*args, **kwargs):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE reference_sync_progress "
                "(reference_name TEXT PRIMARY KEY, status TEXT, completed INTEGER, total INTEGER, message TEXT)"
            )
            conn.execute(
                "INSERT INTO reference_sync_progress VALUES "
                "('products', 'running', 150000, 300000, 'Fetched products')"
            )
        kwargs["log_callback"]("Fetched product page")
        return 300000

    with (
        patch.object(worker, "update_product_catalogue", side_effect=product_sync),
        patch.object(worker, "fetch_and_store_reference_tables", return_value={"warehouses": 1}),
        patch.object(worker, "update_location_catalogue", return_value=3),
        patch.object(worker, "sync_inventory_pricelists", return_value={"price_lists": 4, "price_list_values": 5}),
    ):
        handle(store, _request("syncInventoryReferences", {"accountName": "demo"}, "sync-1"))

    events = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    progress = [event for event in events if event.get("type") == "event" and event.get("event") == "progress"]
    assert progress[0]["data"]["completed"] == 150000
    assert progress[0]["data"]["total"] == 300000
    assert progress[0]["data"]["percent"] == 50


def test_inventory_validation_returns_accepted_and_rejected_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(
        store,
        _request(
            "saveAccount",
            {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
            "save-1",
        ),
    )
    capsys.readouterr()

    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE product_catalogue (productId INTEGER, SKU TEXT, stockTracked INTEGER);
            INSERT INTO product_catalogue VALUES (101, 'GOOD-SKU', 1);
            CREATE TABLE ref_warehouses (warehouseId INTEGER, name TEXT);
            INSERT INTO ref_warehouses VALUES (1, 'Main');
            CREATE TABLE ref_locations (
                locationId INTEGER, warehouseId INTEGER, zoneId INTEGER,
                groupingA TEXT, groupingB TEXT, groupingC TEXT, groupingD TEXT, barcode TEXT
            );
            INSERT INTO ref_locations VALUES (10, 1, NULL, 'A', NULL, NULL, NULL, NULL);
            """
        )

    source = tmp_path / "inventory.csv"
    rejected_rows = "BAD-SKU-0,3,NOT-A-LOCATION,2.00,1\n" + "".join(
        f"BAD-SKU-{index},3,A,2.00,1\n" for index in range(1, 105)
    )
    source.write_text(
        "sku,quantity,locationName,costprice,warehouseId\n"
        "GOOD-SKU,2,A,1.50,1\n"
        + rejected_rows,
        encoding="utf-8",
    )
    handle(
        store,
        _request(
            "validateInventoryFile",
            {"accountName": "demo", "path": str(source)},
            "validate-1",
        ),
    )

    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert result["inserted"] == 1
    assert result["rejected"] == 105
    assert result["totalRows"] == 106
    assert result["validatedPreview"][0]["sku"] == "GOOD-SKU"
    assert result["rejectedPreview"][0]["sku"] == "BAD-SKU-0"
    assert result["rejectedPreview"][0]["category"] == "unmatched_sku; unmatched_location"
    assert "SKU was not found" in result["rejectedPreview"][0]["reason"]
    assert "Location was not found" in result["rejectedPreview"][0]["reason"]
    assert len(result["rejectedPreview"]) == 100

    destination = tmp_path / "saved-exceptions.csv"
    handle(
        store,
        _request(
            "saveInventoryExceptionReport",
            {
                "accountName": "demo",
                "reportPath": result["exceptionReportPath"],
                "destination": str(destination),
            },
            "save-report-1",
        ),
    )
    saved = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert saved["ok"] is True
    with destination.open(encoding="utf-8-sig", newline="") as handle_file:
        full_report = list(csv.DictReader(handle_file))
    assert len(full_report) == 105
    assert full_report[0]["category"] == "unmatched_sku; unmatched_location"
    assert full_report[-1]["sku"] == "BAD-SKU-104"


def test_open_sales_validation_stages_orders_and_preview(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request(
        "saveAccount",
        {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        "save-1",
    ))
    capsys.readouterr()

    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE contact_catalogue (contactId INTEGER, primaryEmail TEXT, isCustomer INTEGER, isSupplier INTEGER);
            INSERT INTO contact_catalogue VALUES (42, 'buyer@example.com', 1, 0);
            CREATE TABLE product_catalogue (productId INTEGER, SKU TEXT);
            INSERT INTO product_catalogue VALUES (101, 'SKU-1');
            CREATE TABLE ref_warehouses (warehouseId INTEGER, name TEXT);
            INSERT INTO ref_warehouses VALUES (1, 'Main');
            CREATE TABLE ref_channels (channelId INTEGER, code TEXT, name TEXT);
            INSERT INTO ref_channels VALUES (2, 'WEB', 'Web');
            CREATE TABLE ref_price_lists (priceListId INTEGER, code TEXT, name TEXT);
            INSERT INTO ref_price_lists VALUES (3, 'GBP', 'GBP Retail');
            CREATE TABLE ref_currencies (isoCode TEXT);
            INSERT INTO ref_currencies VALUES ('GBP');
            CREATE TABLE ref_shipping_methods (shippingMethodId INTEGER, code TEXT, name TEXT);
            INSERT INTO ref_shipping_methods VALUES (4, 'STD', 'Standard');
            CREATE TABLE ref_payment_methods (code TEXT);
            INSERT INTO ref_payment_methods VALUES ('CARD');
            CREATE TABLE ref_order_statuses (statusId INTEGER, code TEXT, name TEXT, rawJson TEXT);
            INSERT INTO ref_order_statuses VALUES (5, 'NEW', 'New', '{"orderTypeCode":"SO"}');
        """)
    source = tmp_path / "open-sales.csv"
    source.write_text(
        "\n".join([
            "order_ref,placed_on,tax_date,delivery_date,customer_email,warehouse,channel,order_status,currency,price_list,exchange_rate,shipping_method,payment_amount,payment_date,payment_ref,payment_method_code,delivery_address_name,delivery_address_line1,delivery_address_line2,delivery_address_line3,delivery_address_line4,delivery_postcode,delivery_country,delivery_telephone,delivery_email,item_name,item_sku,item qty,item_tax_code,row_net,row_tax_amount",
            "SO-1,01/09/2026,01/09/2026,02/09/2026,buyer@example.com,Main,WEB,NEW,GBP,GBP,1,STD,12.00,01/09/2026,PAY-1,CARD,Buyer,Line 1,,,,AB1 2CD,GB,,buyer@example.com,Widget,SKU-1,2,T20,10.00,2.00",
        ]),
        encoding="utf-8",
    )

    handle(store, _request("validateOpenSalesFile", {"accountName": "demo", "path": str(source)}, "sales-validate"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert result["orders"] == 1
    assert result["rows"] == 1
    assert result["preview"]["orders"] == 1
    assert result["preview"]["payments"] == 1
    assert result["preview"]["executionStatus"]["state"] == "checkpointed"

    handle(store, _request("previewOpenSalesRun", {"accountName": "demo"}, "sales-preview"))
    preview = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert preview["previewOrders"][0]["orderRef"] == "SO-1"
    assert preview["referenceCounts"]["validatedOrders"] == 1


def test_open_sales_single_validated_row_posts_payment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request(
        "saveAccount",
        {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        "save-1",
    ))
    capsys.readouterr()

    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE contact_catalogue (contactId INTEGER, primaryEmail TEXT, isCustomer INTEGER, isSupplier INTEGER);
            INSERT INTO contact_catalogue VALUES (42, 'buyer@example.com', 1, 0);
            CREATE TABLE product_catalogue (productId INTEGER, SKU TEXT);
            INSERT INTO product_catalogue VALUES (101, 'SKU-1');
            CREATE TABLE ref_warehouses (warehouseId INTEGER, name TEXT);
            INSERT INTO ref_warehouses VALUES (1, 'Main');
            CREATE TABLE ref_channels (channelId INTEGER, code TEXT, name TEXT);
            INSERT INTO ref_channels VALUES (2, 'WEB', 'Web');
            CREATE TABLE ref_price_lists (priceListId INTEGER, code TEXT, name TEXT);
            INSERT INTO ref_price_lists VALUES (3, 'GBP', 'GBP Retail');
            CREATE TABLE ref_currencies (isoCode TEXT);
            INSERT INTO ref_currencies VALUES ('GBP');
            CREATE TABLE ref_shipping_methods (shippingMethodId INTEGER, code TEXT, name TEXT);
            INSERT INTO ref_shipping_methods VALUES (4, 'STD', 'Standard');
            CREATE TABLE ref_payment_methods (code TEXT);
            INSERT INTO ref_payment_methods VALUES ('CARD');
            CREATE TABLE ref_order_statuses (statusId INTEGER, code TEXT, name TEXT, rawJson TEXT);
            INSERT INTO ref_order_statuses VALUES (5, 'NEW', 'New', '{"orderTypeCode":"SO"}');
        """)
    source = tmp_path / "paid-open-sales.csv"
    source.write_text(
        "\n".join([
            "order_ref,placed_on,tax_date,delivery_date,customer_email,warehouse,channel,order_status,currency,price_list,exchange_rate,shipping_method,payment_amount,payment_date,payment_ref,payment_method_code,delivery_address_name,delivery_address_line1,delivery_address_line2,delivery_address_line3,delivery_address_line4,delivery_postcode,delivery_country,delivery_telephone,delivery_email,item_name,item_sku,item qty,item_tax_code,row_net,row_tax_amount",
            "SO-PAID,01/09/2026,01/09/2026,02/09/2026,buyer@example.com,Main,WEB,NEW,GBP,GBP,1,STD,12.00,01/09/2026,PAY-1,CARD,Buyer,Line 1,,,,AB1 2CD,GB,,buyer@example.com,Widget,SKU-1,2,T20,10.00,2.00",
        ]),
        encoding="utf-8",
    )
    handle(store, _request("validateOpenSalesFile", {"accountName": "demo", "path": str(source)}, "sales-validate-paid"))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["result"]["preview"]["payments"] == 1

    with (
        patch.object(worker, "post_sales_order", return_value=(True, 1001)),
        patch.object(worker, "post_sales_payment", return_value=(True, {"response": 2001})) as post_payment,
    ):
        handle(store, _request("runOpenSalesOrder", {
            "accountName": "demo", "confirmAccountName": "demo"
        }, "sales-live-paid"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert result["paymentState"] == "succeeded"
    post_payment.assert_called_once()
    payload = post_payment.call_args.args[2]
    assert payload["orderId"] == 1001
    assert payload["paymentMethodCode"] == "CARD"
    assert payload["amountPaid"] == 12.0


def test_open_sales_live_retry_reuses_saved_order_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request(
        "saveAccount",
        {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        "save-1",
    ))
    capsys.readouterr()
    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE validated_sales_orders (
                id INTEGER PRIMARY KEY, order_ref TEXT, contactId INTEGER, placed_on TEXT, tax_date TEXT,
                delivery_date TEXT, warehouseId INTEGER, channelId INTEGER, statusId INTEGER, currency TEXT,
                priceListId INTEGER, exchangeRate REAL, shippingMethodId INTEGER, payment_amount REAL,
                payment_date TEXT, payment_ref TEXT, payment_method_code TEXT, orderId INTEGER,
                delivery_name TEXT, delivery_line1 TEXT, delivery_line2 TEXT, delivery_line3 TEXT,
                delivery_line4 TEXT, delivery_postcode TEXT, delivery_country TEXT, delivery_countryIsoCode TEXT,
                delivery_telephone TEXT, delivery_email TEXT, source_csv_headers TEXT, source_csv_filename TEXT
            );
            INSERT INTO validated_sales_orders VALUES (
                1, 'SO-RETRY', 42, '2026-09-01', '2026-09-01', '2026-09-02', 1, 2, 5, 'GBP',
                3, 1, 4, 12.00, '2026-09-01', 'PAY-1', 'CARD', NULL,
                'Buyer', 'Line 1', NULL, NULL, NULL, 'AB1 2CD', 'GB', 'GB', NULL,
                'buyer@example.com', '[]', 'source.csv'
            );
            CREATE TABLE validated_sales_order_rows (
                id INTEGER PRIMARY KEY, order_ref TEXT, line_number INTEGER, item_name TEXT, item_sku TEXT,
                item_qty REAL, item_tax_code TEXT, row_net REAL, row_tax_amount REAL, productId INTEGER,
                rowType TEXT, original_row_json TEXT
            );
            INSERT INTO validated_sales_order_rows VALUES (
                1, 'SO-RETRY', 1, 'Widget', 'SKU-1', 2, 'T20', 10, 2, 101,
                'PRODUCT', '{"order_ref":"SO-RETRY"}'
            );
        """)

    with (
        patch.object(worker, "post_sales_order", return_value=(True, 999)) as create_order,
        patch.object(worker, "post_sales_payment", return_value=(False, None)),
    ):
        handle(store, _request("runOpenSalesOrder", {
            "accountName": "demo", "confirmAccountName": "demo"
        }, "sales-live-1"))
    failed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert failed["error"]["code"] == "write_uncertain"
    create_order.assert_called_once()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT orderId FROM validated_sales_orders").fetchone()[0] == 999

    with (
        patch.object(worker, "post_sales_order") as create_order_again,
        patch.object(worker, "post_sales_payment", return_value=(True, {"response": 1})),
    ):
        handle(store, _request("runOpenSalesOrder", {
            "accountName": "demo", "confirmAccountName": "demo"
        }, "sales-live-2"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    create_order_again.assert_not_called()
    assert result["orderId"] == 999
    assert result["paymentState"] == "succeeded"


def test_open_sales_live_suppresses_legacy_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request(
        "saveAccount",
        {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        "save-1",
    ))
    capsys.readouterr()
    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE validated_sales_orders (
                id INTEGER PRIMARY KEY, order_ref TEXT, contactId INTEGER, placed_on TEXT, tax_date TEXT,
                delivery_date TEXT, warehouseId INTEGER, channelId INTEGER, statusId INTEGER, currency TEXT,
                priceListId INTEGER, exchangeRate REAL, shippingMethodId INTEGER, payment_amount REAL,
                payment_date TEXT, payment_ref TEXT, payment_method_code TEXT, orderId INTEGER,
                delivery_name TEXT, delivery_line1 TEXT, delivery_line2 TEXT, delivery_line3 TEXT,
                delivery_line4 TEXT, delivery_postcode TEXT, delivery_country TEXT, delivery_countryIsoCode TEXT,
                delivery_telephone TEXT, delivery_email TEXT, source_csv_headers TEXT, source_csv_filename TEXT
            );
            INSERT INTO validated_sales_orders VALUES (
                1, 'SO-STDOUT', 42, '2026-09-01', '2026-09-01', '2026-09-02', 1, 2, 5, 'GBP',
                3, 1, 4, 12.00, '2026-09-01', 'PAY-1', 'CARD', NULL,
                'Buyer', 'Line 1', NULL, NULL, NULL, 'AB1 2CD', 'GB', 'GB', NULL,
                'buyer@example.com', '[]', 'source.csv'
            );
            CREATE TABLE validated_sales_order_rows (
                id INTEGER PRIMARY KEY, order_ref TEXT, line_number INTEGER, item_name TEXT, item_sku TEXT,
                item_qty REAL, item_tax_code TEXT, row_net REAL, row_tax_amount REAL, productId INTEGER,
                rowType TEXT, original_row_json TEXT
            );
            INSERT INTO validated_sales_order_rows VALUES (
                1, 'SO-STDOUT', 1, 'Widget', 'SKU-1', 2, 'T20', 10, 2, 101,
                'PRODUCT', '{"order_ref":"SO-STDOUT"}'
            );
        """)

    def noisy_order(*args, **kwargs):
        print("ORDER POST raw legacy line")
        return True, 1003

    def noisy_payment(*args, **kwargs):
        print("PAYMENT POST raw legacy line")
        return True, {"response": 2003}

    with (
        patch.object(worker, "post_sales_order", side_effect=noisy_order),
        patch.object(worker, "post_sales_payment", side_effect=noisy_payment),
    ):
        handle(store, _request("runOpenSalesOrder", {
            "accountName": "demo", "confirmAccountName": "demo"
        }, "sales-live-stdout"))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["paymentState"] == "succeeded"


def test_open_sales_payment_amount_without_details_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request(
        "saveAccount",
        {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        "save-1",
    ))
    capsys.readouterr()
    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE validated_sales_orders (
                id INTEGER PRIMARY KEY, order_ref TEXT, contactId INTEGER, placed_on TEXT, tax_date TEXT,
                delivery_date TEXT, warehouseId INTEGER, channelId INTEGER, statusId INTEGER, currency TEXT,
                priceListId INTEGER, exchangeRate REAL, shippingMethodId INTEGER, payment_amount REAL,
                payment_date TEXT, payment_ref TEXT, payment_method_code TEXT, orderId INTEGER,
                delivery_name TEXT, delivery_line1 TEXT, delivery_line2 TEXT, delivery_line3 TEXT,
                delivery_line4 TEXT, delivery_postcode TEXT, delivery_country TEXT, delivery_countryIsoCode TEXT,
                delivery_telephone TEXT, delivery_email TEXT, source_csv_headers TEXT, source_csv_filename TEXT
            );
            INSERT INTO validated_sales_orders VALUES (
                1, 'SO-MISSING-PAYMENT', 42, '2026-09-01', '2026-09-01', '2026-09-02', 1, 2, 5, 'GBP',
                3, 1, 4, 12.00, NULL, 'PAY-1', NULL, NULL,
                'Buyer', 'Line 1', NULL, NULL, NULL, 'AB1 2CD', 'GB', 'GB', NULL,
                'buyer@example.com', '[]', 'source.csv'
            );
            CREATE TABLE validated_sales_order_rows (
                id INTEGER PRIMARY KEY, order_ref TEXT, line_number INTEGER, item_name TEXT, item_sku TEXT,
                item_qty REAL, item_tax_code TEXT, row_net REAL, row_tax_amount REAL, productId INTEGER,
                rowType TEXT, original_row_json TEXT
            );
            INSERT INTO validated_sales_order_rows VALUES (
                1, 'SO-MISSING-PAYMENT', 1, 'Widget', 'SKU-1', 2, 'T20', 10, 2, 101,
                'PRODUCT', '{"order_ref":"SO-MISSING-PAYMENT"}'
            );
        """)

    with (
        patch.object(worker, "post_sales_order", return_value=(True, 1002)),
        patch.object(worker, "post_sales_payment") as post_payment,
    ):
        handle(store, _request("runOpenSalesOrder", {
            "accountName": "demo", "confirmAccountName": "demo"
        }, "sales-live-missing-payment"))
    failed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert failed["error"]["code"] == "missing_payment_details"
    post_payment.assert_not_called()


def test_open_sales_template_and_reference_sync_parity(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request(
        "saveAccount",
        {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        "save-1",
    ))
    capsys.readouterr()

    template = tmp_path / "bp_sales_import.csv"
    handle(store, _request("saveOpenSalesTemplate", {"destination": str(template)}, "sales-template"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    with template.open(encoding="utf-8", newline="") as file:
        headers = next(csv.reader(file))
    assert result["headers"] == headers
    assert headers[:3] == ["order_ref", "placed_on", "tax_date"]

    def reference_sync(*args, **kwargs):
        with sqlite3.connect(args[3]) as conn:
            conn.execute("CREATE TABLE ref_channels (channelId INTEGER)")
            conn.execute("INSERT INTO ref_channels VALUES (1)")
        return {"channels": 1}

    def contacts_sync(*args, **kwargs):
        with sqlite3.connect(args[1]) as conn:
            conn.execute("CREATE TABLE contact_catalogue (contactId INTEGER)")
            conn.execute("INSERT INTO contact_catalogue VALUES (2)")
        return 1

    def products_sync(*args, **kwargs):
        with sqlite3.connect(args[1]) as conn:
            conn.execute("CREATE TABLE product_catalogue (productId INTEGER)")
            conn.execute("INSERT INTO product_catalogue VALUES (3)")
        return 1

    with (
        patch.object(worker, "fetch_and_store_reference_tables", side_effect=reference_sync) as refs,
        patch.object(worker, "update_contact_catalogue", side_effect=contacts_sync) as contacts,
        patch.object(worker, "update_product_catalogue", side_effect=products_sync) as products,
    ):
        handle(store, _request("syncOpenSalesReferences", {"accountName": "demo", "mode": "all"}, "sales-refs"))
    sync = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    refs.assert_called_once()
    contacts.assert_called_once()
    products.assert_called_once()
    assert sync["results"] == {"reference": {"channels": 1}, "contacts": 1, "products": 1}
    assert sync["referenceCounts"]["channels"] == 1
    assert sync["referenceCounts"]["customers"] == 1
    assert sync["referenceCounts"]["products"] == 1


def test_open_sales_reference_sync_compacts_large_logs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request(
        "saveAccount",
        {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"},
        "save-1",
    ))
    capsys.readouterr()

    def noisy_reference_sync(*args, **kwargs):
        log_callback = kwargs["log_callback"]
        for index in range(200):
            log_callback(f"Reference row {index} " + ("x" * 1000))
        with sqlite3.connect(args[3]) as conn:
            conn.execute("CREATE TABLE ref_channels (channelId INTEGER)")
            conn.execute("INSERT INTO ref_channels VALUES (1)")
        return {"channels": 1}

    with (
        patch.object(worker, "fetch_and_store_reference_tables", side_effect=noisy_reference_sync),
        patch.object(worker, "update_contact_catalogue", return_value=0),
        patch.object(worker, "update_product_catalogue", return_value=0),
    ):
        handle(store, _request("syncOpenSalesReferences", {"accountName": "demo", "mode": "reference"}, "sales-refs-big"))
    line = capsys.readouterr().out.splitlines()[-1]
    assert len(line.encode("utf-8")) < worker.MAX_MESSAGE_BYTES
    result = json.loads(line)["result"]
    assert result["results"] == {"reference": {"channels": 1}}
    assert len(result["logs"]) < 25


def test_inventory_validation_uses_account_price_list_and_blank_option(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    handle(store, _request("saveAccount", {"accountName": "demo", "appRef": "app", "token": "secret", "region": "euw1"}, "save-1"))
    capsys.readouterr()
    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE product_catalogue (productId INTEGER, SKU TEXT, stockTracked INTEGER);
            INSERT INTO product_catalogue VALUES (101, 'GOOD-SKU', 1);
            CREATE TABLE ref_warehouses (warehouseId INTEGER, name TEXT);
            INSERT INTO ref_warehouses VALUES (1, 'Main');
            CREATE TABLE ref_locations (locationId INTEGER, warehouseId INTEGER, zoneId INTEGER,
                groupingA TEXT, groupingB TEXT, groupingC TEXT, groupingD TEXT, barcode TEXT);
            INSERT INTO ref_locations VALUES (10, 1, NULL, 'A', NULL, NULL, NULL, NULL);
            CREATE TABLE ref_price_lists (priceListId INTEGER, name TEXT, code TEXT);
            INSERT INTO ref_price_lists VALUES (7, 'Cost list', 'COST');
            CREATE TABLE ref_price_list_values (productId INTEGER, priceListId INTEGER, value REAL);
            INSERT INTO ref_price_list_values VALUES (101, 7, 12.5);
        """)
    handle(store, _request("inventoryPriceLists", {"accountName": "demo"}, "lists-1"))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["result"]["priceLists"] == [
        {"id": 7, "name": "Cost list"}
    ]
    source = tmp_path / "inventory.csv"
    source.write_text("sku,quantity,locationName,warehouseId\nGOOD-SKU,,A,1\n", encoding="utf-8")
    handle(store, _request("validateInventoryFile", {
        "accountName": "demo", "path": str(source), "allowZeroBlanks": True, "priceListId": 7
    }, "validate-1"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert (result["inserted"], result["rejected"], result["priceListId"]) == (1, 0, 7)
    assert result["validatedPreview"][0]["quantity"] == 0.0
    assert result["validatedPreview"][0]["costprice"] == 12.5

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM ref_price_list_values WHERE productId = 101 AND priceListId = 7")
    handle(store, _request("validateInventoryFile", {
        "accountName": "demo", "path": str(source), "allowZeroBlanks": True, "priceListId": 7
    }, "validate-2"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert (result["inserted"], result["rejected"]) == (1, 0)
    assert result["validatedPreview"][0]["costprice"] == 0.0

    handle(store, _request("previewInventoryRun", {
        "accountName": "demo", "validationJobId": result["validationJobId"]
    }, "preview-no-currency"))
    missing_currency = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert missing_currency["error"]["code"] == "currency_missing"

    worker.upsert_account_metadata(store, "demo", "euw1", base_currency="GBP")
    with patch.object(worker.requests, "post") as post:
        handle(store, _request("previewInventoryRun", {
            "accountName": "demo", "validationJobId": result["validationJobId"]
        }, "preview-1"))
    post.assert_not_called()
    preview = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert (preview["corrections"], preview["batches"], preview["warehouses"]) == (1, 1, 1)
    assert preview["writeEnabled"] is False
    assert len(preview["reportSha256"]) == 64
    assert preview["samplePayload"] == {
        "accountName": "demo",
        "warehouseId": "1",
        "corrections": [{
            "productId": 101, "quantity": 0.0, "locationId": 10,
            "cost": {"currency": "GBP", "value": 0.0}, "reason": "Stock Sync",
        }],
    }
    with open(preview["reportPath"], encoding="utf-8") as report:
        assert [json.loads(line) for line in report] == [preview["samplePayload"]]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT processed FROM validated_inventory").fetchone()[0] == 0

    destination = tmp_path / "dry-run.jsonl"
    handle(store, _request("saveInventoryRunPreview", {
        "accountName": "demo", "reportPath": preview["reportPath"],
        "reportSha256": preview["reportSha256"], "destination": str(destination)
    }, "save-preview-1"))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True
    assert json.loads(destination.read_text(encoding="utf-8")) == preview["samplePayload"]

    with open(preview["reportPath"], "a", encoding="utf-8") as report:
        report.write("{}\n")
    handle(store, _request("saveInventoryRunPreview", {
        "accountName": "demo", "reportPath": preview["reportPath"],
        "reportSha256": preview["reportSha256"], "destination": str(destination)
    }, "save-preview-changed"))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "report_changed"

    handle(store, _request("previewInventoryRun", {
        "accountName": "demo", "validationJobId": "not-the-latest"
    }, "preview-stale"))
    stale = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert stale["error"]["code"] == "stale_validation"


def test_inventory_dry_run_batches_by_warehouse_and_skips_processed_rows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    worker.upsert_account_metadata(store, "demo", "euw1", base_currency="GBP")
    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE validated_inventory (id INTEGER PRIMARY KEY, productId INTEGER, "
                     "quantity REAL, locationId INTEGER, costprice REAL, warehouseId TEXT, processed INTEGER)")
        conn.executemany(
            "INSERT INTO validated_inventory VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(index, 100 + index, 1, 10, 2.5, "1", 0) for index in range(1, 52)]
            + [(52, 152, 2, 20, 3.5, "2", 0), (53, 153, 2, 20, 3.5, "2", 1)],
        )
    worker.update_job(store, "validated-demo", kind="inventory_validation", state="succeeded", dataset_id="demo")
    handle(store, _request("previewInventoryRun", {
        "accountName": "demo", "validationJobId": "validated-demo"
    }, "preview-batches"))
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    assert (result["corrections"], result["batches"], result["warehouses"]) == (52, 3, 2)
    assert len(result["samplePayload"]["corrections"]) == 10
    with open(result["reportPath"], encoding="utf-8") as report:
        payloads = [json.loads(line) for line in report]
    assert [(payload["warehouseId"], len(payload["corrections"])) for payload in payloads] == [
        ("1", 50), ("1", 1), ("2", 1)
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT SUM(processed) FROM validated_inventory").fetchone()[0] == 1

    worker.upsert_account_metadata(store, "demo", "euw1", references_synced=True)
    handle(store, _request("previewInventoryRun", {
        "accountName": "demo", "validationJobId": "validated-demo"
    }, "preview-after-refresh"))
    refreshed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert refreshed["error"]["code"] == "stale_validation"


def _live_run_fixture(tmp_path, monkeypatch, capsys, quantities=(1, 2)):
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("TACOS_CREDENTIAL_BACKEND", "sqlite_plaintext")
    store = app_store()
    worker.save_credential("demo", "app", "secret")
    worker.upsert_account_metadata(store, "demo", "euw1", base_currency="GBP")
    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE validated_inventory (id INTEGER PRIMARY KEY, productId INTEGER, "
                     "quantity REAL, locationId INTEGER, costprice REAL, warehouseId TEXT, processed INTEGER)")
        conn.executemany("INSERT INTO validated_inventory VALUES (?, ?, ?, ?, ?, ?, 0)", [
            (index, 100 + index, quantity, 10, 2.5, str(index))
            for index, quantity in enumerate(quantities, 1)
        ])
    worker.update_job(store, "validated-demo", kind="inventory_validation", state="succeeded", dataset_id="demo")
    handle(store, _request("previewInventoryRun", {
        "accountName": "demo", "validationJobId": "validated-demo"
    }, "preview-live"))
    preview = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
    params = {
        "accountName": "demo", "confirmAccountName": "demo",
        "validationJobId": preview["validationJobId"],
        "previewJobId": preview["previewJobId"], "reportSha256": preview["reportSha256"],
    }
    return store, db_path, params


def test_live_inventory_batches_confirm_and_mark_only_successful_rows(tmp_path, monkeypatch, capsys):
    store, db_path, params = _live_run_fixture(tmp_path, monkeypatch, capsys)
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: {"response": [901]})
    with patch.object(worker.requests, "post", return_value=response) as post:
        handle(store, _request("runInventoryBatch", {**params, "confirmAccountName": "other"}, "wrong-account"))
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "confirmation_required"
        post.assert_not_called()
        handle(store, _request("runInventoryBatch", params, "live-1"))
        first = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
        assert (first["completedBatches"], first["totalBatches"], first["done"]) == (1, 2, False)
        assert post.call_args.kwargs["json"] == {"corrections": [{
            "productId": 101, "quantity": 1, "locationId": 10,
            "cost": {"currency": "GBP", "value": 2.5}, "reason": "Stock Sync",
        }]}
        assert post.call_args.kwargs["timeout"] == (10, 60)
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT processed FROM validated_inventory ORDER BY id").fetchall() == [(1,), (0,)]
        handle(app_store(), _request("inventoryRunResume", {"accountName": "demo"}, "resume-live"))
        resume = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
        assert resume["completedBatches"] == 1
        assert resume["preview"]["reportSha256"] == params["reportSha256"]
        handle(store, _request("previewInventoryRun", {
            "accountName": "demo", "validationJobId": "validated-demo"
        }, "new-preview-during-live"))
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "resume_required"
        handle(store, _request("validateInventoryFile", {"accountName": "demo", "path": "unused.csv"}, "revalidate-during-live"))
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "resume_required"
        handle(store, _request("runInventoryBatch", params, "live-2"))
        second = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
        assert second["done"] is True
        assert post.call_count == 2
        handle(store, _request("inventoryLiveStatus", {"accountName": "demo"}, "live-status"))
        status = json.loads(capsys.readouterr().out.splitlines()[-1])["result"]
        assert len(status["batches"]) == 2
        assert all(batch["state"] == "succeeded" and batch["goodsNoteIds"] == [901] for batch in status["batches"])
        handle(store, _request("runInventoryBatch", params, "live-repeat"))
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "already_run"
        assert post.call_count == 2


def test_live_inventory_timeout_blocks_retry_and_preserves_rows(tmp_path, monkeypatch, capsys):
    store, db_path, params = _live_run_fixture(tmp_path, monkeypatch, capsys, quantities=(1,))
    with patch.object(worker.requests, "post", side_effect=worker.requests.Timeout) as post:
        handle(store, _request("runInventoryBatch", params, "live-timeout"))
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "write_uncertain"
        handle(store, _request("runInventoryBatch", params, "live-retry"))
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "reconciliation_required"
        assert post.call_count == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT processed FROM validated_inventory").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM inventory_live_batches").fetchone()[0] == "unknown"


def test_live_inventory_rechecks_staged_rows_before_post(tmp_path, monkeypatch, capsys):
    store, db_path, params = _live_run_fixture(tmp_path, monkeypatch, capsys, quantities=(1,))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE validated_inventory SET quantity = 9 WHERE id = 1")
    with patch.object(worker.requests, "post") as post:
        handle(store, _request("runInventoryBatch", params, "live-changed"))
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "staged_rows_changed"
        post.assert_not_called()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM inventory_live_runs").fetchone()[0] == 0


def test_dry_run_rejects_fractional_stock_correction_quantity(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TACOS_DESKTOP_DATA_DIR", str(tmp_path / "appdata"))
    store = app_store()
    worker.upsert_account_metadata(store, "demo", "euw1", base_currency="GBP")
    db_path = worker.account_data_db(store, "demo")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE validated_inventory (id INTEGER PRIMARY KEY, productId INTEGER, "
                     "quantity REAL, locationId INTEGER, costprice REAL, warehouseId TEXT, processed INTEGER)")
        conn.execute("INSERT INTO validated_inventory VALUES (1, 101, 1.5, 10, 2.5, '1', 0)")
    worker.update_job(store, "validated-demo", kind="inventory_validation", state="succeeded", dataset_id="demo")
    handle(store, _request("previewInventoryRun", {
        "accountName": "demo", "validationJobId": "validated-demo"
    }, "preview-fractional"))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"]["code"] == "fractional_quantity"
