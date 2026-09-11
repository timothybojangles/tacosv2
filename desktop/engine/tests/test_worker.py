import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tacos_engine import worker
from tacos_engine.worker import app_store, handle


def _request(method, params=None, request_id="request-1"):
    return {"protocolVersion": 1, "id": request_id, "method": method, "params": params or {}}


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
    source.write_text(
        "sku,quantity,locationName,costprice,warehouseId\n"
        "GOOD-SKU,2,A,1.50,1\n"
        "BAD-SKU,3,A,2.00,1\n",
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
    assert result["rejected"] == 1
    assert result["totalRows"] == 2
    assert result["validatedPreview"][0]["sku"] == "GOOD-SKU"
    assert result["rejectedPreview"][0]["sku"] == "BAD-SKU"
    assert result["rejectedPreview"][0]["category"] == "unmatched_sku"
