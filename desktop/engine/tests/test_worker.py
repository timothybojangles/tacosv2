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
