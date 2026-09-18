import csv
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tacos_engine import worker
from tacos_engine.worker import app_store, handle


def _request(method, params=None, request_id="request-1"):
    return {"protocolVersion": 1, "id": request_id, "method": method, "params": params or {}}


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
