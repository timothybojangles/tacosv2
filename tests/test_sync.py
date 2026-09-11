from types import SimpleNamespace
from unittest.mock import patch

import sync


def test_stock_sync_uses_global_stock_correction_batch_size():
    inventory = [
        {
            "id": index,
            "sku": f"SKU-{index}",
            "quantity": 1,
            "locationId": 10,
            "costprice": 2,
            "warehouseId": 3,
            "productId": 100 + index,
        }
        for index in range(5)
    ]
    credentials = SimpleNamespace(app_ref="app", token="token", region="euw1")
    with (
        patch.object(sync, "ensure_account_binding"),
        patch.object(sync, "fetch_credentials_with_currency", return_value=(credentials, "GBP")),
        patch.object(sync, "load_validated_inventory", return_value=inventory),
        patch.object(sync, "get_settings", return_value=SimpleNamespace(stock_correction_batch_size=2)),
        patch.object(sync, "send_batch", return_value={"ok": False, "error_messages_by_product_id": {}}) as send,
        patch.object(sync, "write_failed_batches"),
    ):
        sync.main("account", "inventory.db")

    assert [len(call.args[0]["corrections"]) for call in send.call_args_list] == [2, 2, 1]
