import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import brightpearl.ic_training_helper as helper


def _credentials(region="euw1"):
    return SimpleNamespace(region=region, headers={"token": "test"})


def test_create_customer_uses_region_country_and_address_id(tmp_path):
    db_path = str(tmp_path / "data.db")
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "_post", side_effect=[123, 288]) as post,
    ):
        assert helper.create_dummy_customer("account", db_path) == 288

    assert post.call_args_list[0].args[2]["countryIsoCode"] == "GBR"
    assert post.call_args_list[1].args[2]["postAddressIds"] == {
        "DEF": 123,
        "BIL": 123,
        "DEL": 123,
    }
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT contactId FROM ref_dummy").fetchone() == (288,)


def test_create_product_uses_synced_defaults_and_stores_id(tmp_path):
    db_path = str(tmp_path / "data.db")
    helper.ensure_dummy_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE ref_brands (brandId INTEGER, brandName TEXT)")
        conn.execute("INSERT INTO ref_brands VALUES (5, 'Other')")
        conn.execute("CREATE TABLE ref_product_category (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO ref_product_category VALUES (9, 'Other')")
        conn.execute("CREATE TABLE ref_configuration (id INTEGER, rawJson TEXT)")
        conn.execute(
            "INSERT INTO ref_configuration VALUES (1, ?)",
            (json.dumps({"defaultTaxRateId": "1"}),),
        )
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "_post", return_value=1045) as post,
    ):
        assert helper.create_dummy_product("account", db_path) == 1045

    payload = post.call_args.args[2]
    assert payload["brandId"] == 5
    assert payload["financialDetails"]["taxCode"]["id"] == 1
    assert payload["salesChannels"][0]["categories"] == [{"categoryCode": "9"}]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT productId FROM ref_dummy").fetchone() == (1045,)


def test_do_it_all_runs_actions_in_order(tmp_path):
    calls = []

    def action(name, result):
        def run(*args, **kwargs):
            calls.append(name)
            return result
        return run

    with (
        patch.object(helper, "check_defaults", action("defaults", {})),
        patch.object(helper, "create_dummy_customer", action("customer", 1)),
        patch.object(helper, "create_dummy_product", action("product", 2)),
        patch.object(helper, "create_dummy_shipping_method", action("shipping", 3)),
    ):
        helper.do_it_all("account", str(tmp_path / "data.db"))

    assert calls == ["defaults", "customer", "product", "shipping"]


def _seed_order_data(db_path):
    helper.ensure_dummy_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE ref_dummy SET contactId=11, productId=22, shippingMethodId=33")
        conn.execute("CREATE TABLE ref_configuration (id INTEGER, rawJson TEXT)")
        conn.execute("INSERT INTO ref_configuration VALUES (1, ?)", (json.dumps({
            "newSalesOrderStatusId": "1", "baseCurrencyCode": "GBP",
            "shippingNominalCode": "5000"
        }),))
        conn.execute(
            "CREATE TABLE ref_payment_methods "
            "(paymentMethodId INTEGER, code TEXT, name TEXT, rawJson TEXT)"
        )
        conn.execute(
            "INSERT INTO ref_payment_methods VALUES (?, ?, ?, ?)",
            (7, "PAYPALGBP", "PayPal GBP", json.dumps({
                "paymentMethodId": 7, "code": "PAYPALGBP", "name": "PayPal GBP",
                "active": True, "currency": "GBP", "bankAccountNominalCode": "1200"
            })),
        )


def test_create_sales_order_uses_saved_ids_configuration_and_region(tmp_path):
    db_path = str(tmp_path / "data.db")
    _seed_order_data(db_path)
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "_post", return_value=42) as post,
        patch.object(helper, "post_payment") as payment_post,
    ):
        assert helper.create_sales_order("account", db_path) == 42

    url, _, payload = post.call_args.args
    assert url.endswith("/order-service/sales-order/")
    assert payload["customer"] == {"id": 11}
    assert payload["channelId"] == 1
    assert payload["delivery"]["shippingMethodId"] == 33
    assert payload["delivery"]["address"]["countryIsoCode"] == "GBR"
    assert payload["rows"][0]["productId"] == 22
    assert payload["rows"][0]["taxCode"] == "T20"
    assert payload["rows"][1]["nominalCode"] == "5000"
    assert payload["ref"].isdigit()
    payment_payload = payment_post.call_args.args[2]
    assert payment_post.call_args.args[0].endswith("/public-api/account")
    assert payment_payload == {
        "transactionRef": payload["ref"],
        "transactionCode": "",
        "paymentMethodCode": "PAYPALGBP",
        "paymentType": "RECEIPT",
        "orderId": 42,
        "currencyIsoCode": "GBP",
        "exchangeRate": 1,
        "amountPaid": 18.0,
        "paymentDate": payload["placedOn"],
        "journalRef": "Dummy order 42 paid",
    }


def test_paypal_method_must_be_active_and_match_base_currency(tmp_path):
    db_path = str(tmp_path / "data.db")
    _seed_order_data(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE ref_payment_methods SET rawJson = ?", (json.dumps({
            "code": "PAYPALGBP", "name": "PAYPAL GBP", "active": False,
            "currency": "GBP"
        }),))
        conn.execute("INSERT INTO ref_payment_methods VALUES (?, ?, ?, ?)", (
            8, "PAYPALUSD", "paypal USD", json.dumps({
                "code": "PAYPALUSD", "name": "paypal USD", "active": True,
                "currency": "USD"
            })
        ))

    with patch.object(helper, "ensure_account_binding"), patch.object(
        helper, "fetch_credentials", return_value=_credentials()
    ):
        try:
            helper.create_sales_order("account", db_path)
        except ValueError as exc:
            assert str(exc) == "No active PayPal payment method found for GBP."
        else:
            raise AssertionError("Expected a missing PayPal payment method error")


def test_paypal_method_supports_compact_raw_json_rows(tmp_path):
    db_path = str(tmp_path / "data.db")
    _seed_order_data(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE ref_payment_methods SET code = ?, name = ?, rawJson = ?",
            ("1240 Paypal Account", "1240", json.dumps(
                [6, "1240", "1240 Paypal Account", True, "GBP", "1240"]
            )),
        )

    assert helper._paypal_payment_method_code(db_path, "GBP") == "1240"


def test_quick_stock_uses_entered_quantity_and_configuration(tmp_path):
    db_path = str(tmp_path / "data.db")
    _seed_order_data(db_path)
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "_post", return_value=99) as post,
    ):
        helper.quick_stock("account", db_path, quantity="12.5")

    payload = post.call_args.args[2]
    correction = payload["corrections"][0]
    assert correction["quantity"] == 12.5
    assert correction["productId"] == 22
    assert correction["cost"] == {"currency": "GBP", "value": 20}


def test_check_defaults_saves_existing_magical_shipping_method(tmp_path):
    db_path = str(tmp_path / "data.db")

    def sync_references(*args, **kwargs):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE ref_shipping_methods "
                "(shippingMethodId INTEGER, name TEXT, code TEXT, rawJson TEXT)"
            )
            conn.execute(
                "INSERT INTO ref_shipping_methods VALUES (77, 'Magical shipping method', 'MSM', '{}')"
            )
        return {"shipping_methods": 1}

    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "fetch_and_store_reference_tables", side_effect=sync_references),
        patch.object(helper, "sync_product_reference_subset", return_value={}),
        patch.object(helper, "send_request", return_value=(None, 0, 0)),
    ):
        helper.check_defaults("account", db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT shippingMethodId FROM ref_dummy").fetchone() == (77,)


def test_check_defaults_discovers_all_existing_dummy_records(tmp_path):
    db_path = str(tmp_path / "data.db")

    def sync_references(*args, **kwargs):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE ref_shipping_methods "
                "(shippingMethodId INTEGER, name TEXT, code TEXT, rawJson TEXT)"
            )
            conn.execute(
                "INSERT INTO ref_shipping_methods VALUES "
                "(77, 'Magical shipping method', 'MSM', '{}')"
            )
        return {"shipping_methods": 1}

    contact_response = json.dumps({"response": {"results": [
        [41, "", "", "", "Testy", "McTestface"]
    ]}})
    product_response = json.dumps({"response": {"results": [
        [52, "Creativity Lube", "CRL001"]
    ]}})
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "fetch_and_store_reference_tables", side_effect=sync_references),
        patch.object(helper, "sync_product_reference_subset", return_value={}),
        patch.object(helper, "send_request", side_effect=[
            (contact_response, 0, 100), (product_response, 0, 100)
        ]),
    ):
        result = helper.check_defaults("account", db_path)

    assert result["dummy_records"] == 3
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT contactId, firstName, lastName, productId, sku, productName, "
            "shippingMethodId FROM ref_dummy"
        ).fetchone() == (
            41, "Testy", "McTestface", 52, "CRL001", "Creativity Lube", 77
        )


def test_create_shipping_method_reuses_synced_method_without_posting(tmp_path):
    db_path = str(tmp_path / "data.db")
    helper.ensure_dummy_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE ref_shipping_methods "
            "(shippingMethodId INTEGER, name TEXT, code TEXT, rawJson TEXT)"
        )
        conn.execute(
            "INSERT INTO ref_shipping_methods VALUES "
            "(77, 'Magical shipping method', 'MSM', '{}')"
        )
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "_post") as post,
    ):
        assert helper.create_dummy_shipping_method("account", db_path) == 77

    post.assert_not_called()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT shippingMethodId FROM ref_dummy").fetchone() == (77,)


def test_training_reference_values_returns_saved_data_and_region_defaults(tmp_path):
    db_path = str(tmp_path / "data.db")
    helper.ensure_dummy_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE ref_dummy SET contactId=11, firstName='Testy', lastName='McTestface', "
            "productId=22, sku='CRL001', productName='Creativity Lube', shippingMethodId=33"
        )
        conn.execute("CREATE TABLE ref_brands (brandId INTEGER, brandName TEXT)")
        conn.execute("INSERT INTO ref_brands VALUES (5, 'Other')")
        conn.execute("CREATE TABLE ref_product_category (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO ref_product_category VALUES (9, 'Other')")
        conn.execute("CREATE TABLE ref_configuration (id INTEGER, rawJson TEXT)")
        conn.execute("INSERT INTO ref_configuration VALUES (1, ?)", (json.dumps({
            "defaultTaxRate": "20", "newSalesOrderStatusId": "4",
            "baseCurrencyCode": "GBP", "shippingNominalCode": "5000"
        }),))

    with patch.object(helper, "fetch_credentials", return_value=_credentials("euw1")):
        values = helper.training_reference_values("account", db_path)

    assert values == {
        "categoryId": 9, "brandId": 5, "defaultTaxRate": "20",
        "contactId": 11, "firstName": "Testy", "lastName": "McTestface",
        "productId": 22, "sku": "CRL001", "productName": "Creativity Lube",
        "channelId": 1, "statusId": "4", "baseCurrencyCode": "GBP",
        "countryIsoCode": "GBR", "taxCode": "T20", "shippingNominalCode": "5000",
        "shippingMethodId": 33,
    }


def test_training_reference_values_leaves_unavailable_values_blank(tmp_path):
    db_path = str(tmp_path / "data.db")
    with patch.object(helper, "fetch_credentials", return_value=_credentials("unknown")):
        values = helper.training_reference_values("account", db_path)

    assert values["contactId"] == ""
    assert values["channelId"] == ""
    assert values["countryIsoCode"] == ""


def test_inventory_reference_values_counts_tables_and_hides_two_skus(tmp_path):
    db_path = str(tmp_path / "data.db")
    with sqlite3.connect(db_path) as conn:
        for table, count in (("product_catalogue", 7), ("ref_warehouses", 2),
                             ("ref_locations", 5), ("ref_price_lists", 3),
                             ("ref_price_list_values", 11)):
            conn.execute(f"CREATE TABLE {table} (id INTEGER)")
            conn.executemany(f"INSERT INTO {table} VALUES (?)", [(i,) for i in range(count)])
    assert helper.inventory_reference_values(db_path) == {
        "skus": 5, "warehouses": 2, "locations": 5,
        "pricelists": 3, "pricelist_entries": 11,
    }


def test_allocate_random_inventory_excludes_hidden_untracked_products_and_quarantine(tmp_path):
    db_path = str(tmp_path / "data.db")
    _seed_order_data(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE product_catalogue (productId INTEGER, stockTracked INTEGER)")
        conn.executemany("INSERT INTO product_catalogue VALUES (?, ?)", [
            (1000, 1), (1001, 1), (2, 1), (3, 1), (4, 1), (5, 0),
        ])
        conn.execute("CREATE TABLE ref_locations (locationId INTEGER, warehouseId INTEGER, "
                     "groupingA TEXT, groupingB TEXT, groupingC TEXT, groupingD TEXT)")
        conn.executemany("INSERT INTO ref_locations VALUES (?, ?, ?, '', '', '')", [
            (10, 1, "PICK"), (20, 2, "BULK"), (30, 1, "QUARANTINE")])
    rng = SimpleNamespace(shuffle=lambda rows: None, randint=lambda low, high: 75)
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "_post", return_value={}) as post,
    ):
        assert helper.allocate_random_inventory("account", db_path, generic_value="9", rng=rng) == 3

    corrections = [item for call in post.call_args_list for item in call.args[2]["corrections"]]
    assert {item["productId"] for item in corrections} == {2, 3, 4}
    assert [item["locationId"] for item in corrections] == [10, 10, 20]
    assert all(item["quantity"] == 75 for item in corrections)
    assert all(item["cost"] == {"currency": "GBP", "value": 9} for item in corrections)


def test_allocate_random_inventory_uses_generic_value_for_missing_pricelist_value(tmp_path):
    db_path = str(tmp_path / "data.db")
    _seed_order_data(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE product_catalogue (productId INTEGER, stockTracked INTEGER)")
        conn.executemany("INSERT INTO product_catalogue VALUES (?, 1)", [(2,), (3,)])
        conn.execute("CREATE TABLE ref_locations (locationId INTEGER, warehouseId INTEGER, "
                     "groupingA TEXT, groupingB TEXT, groupingC TEXT, groupingD TEXT)")
        conn.execute("INSERT INTO ref_locations VALUES (10, 1, 'PICK', '', '', '')")
        conn.execute("CREATE TABLE ref_price_list_values "
                     "(productId INTEGER, priceListId INTEGER, value REAL)")
        conn.execute("INSERT INTO ref_price_list_values VALUES (2, 7, 12.5)")
    rng = SimpleNamespace(shuffle=lambda rows: None, randint=lambda low, high: 75)
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "_post", return_value={}) as post,
    ):
        helper.allocate_random_inventory(
            "account", db_path, price_list_id=7, generic_value="9", rng=rng)

    corrections = post.call_args.args[2]["corrections"]
    costs = {item["productId"]: item["cost"]["value"] for item in corrections}
    assert costs == {2: 12.5, 3: 9}


def test_allocate_random_inventory_uses_global_stock_correction_batch_size(tmp_path):
    db_path = str(tmp_path / "data.db")
    _seed_order_data(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE product_catalogue (productId INTEGER, stockTracked INTEGER)")
        conn.executemany("INSERT INTO product_catalogue VALUES (?, 1)", [(2,), (3,), (4,)])
        conn.execute("CREATE TABLE ref_locations (locationId INTEGER, warehouseId INTEGER, "
                     "groupingA TEXT, groupingB TEXT, groupingC TEXT, groupingD TEXT)")
        conn.execute("INSERT INTO ref_locations VALUES (10, 1, 'PICK', '', '', '')")
    rng = SimpleNamespace(shuffle=lambda rows: None, randint=lambda low, high: 75)
    progress = []
    with (
        patch.object(helper, "ensure_account_binding"),
        patch.object(helper, "fetch_credentials", return_value=_credentials()),
        patch.object(helper, "get_settings", return_value=SimpleNamespace(stock_correction_batch_size=2)),
        patch.object(helper, "_post", return_value={}) as post,
    ):
        assert helper.allocate_random_inventory(
            "account", db_path, generic_value="9", rng=rng,
            progress_callback=lambda done, total: progress.append((done, total)),
        ) == 3

    assert [len(call.args[2]["corrections"]) for call in post.call_args_list] == [2, 1]
    assert progress == [(2, 3), (3, 3)]
