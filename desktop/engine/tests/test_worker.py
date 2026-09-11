import json
from pathlib import Path

from tacos_engine.worker import app_store, handle


def test_worker_imports_and_pages_csv(tmp_path, monkeypatch, capsys):
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

