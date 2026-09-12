"""Create predictable Brightpearl records for implementation-consultant training."""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from .common import (
    connect_sqlite,
    ensure_account_binding,
    fetch_credentials,
    log,
    log_payload_exchange,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)
from .performance import estimate_record_count, record_api_update
from .product_import import sync_product_reference_subset
from .settings import get_settings, get_upload_retry_settings
from reference_data import fetch_and_store_reference_tables


def ensure_dummy_table(db_path: str) -> None:
    """Create the one-row table used to retain IDs for later training steps."""
    conn = connect_sqlite(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_dummy (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            contactId INTEGER,
            firstName TEXT,
            lastName TEXT,
            productId INTEGER,
            sku TEXT,
            productName TEXT,
            shippingMethodId INTEGER
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ref_dummy)")}
    for name, data_type in (
        ("firstName", "TEXT"), ("lastName", "TEXT"),
        ("sku", "TEXT"), ("productName", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE ref_dummy ADD COLUMN {name} {data_type}")
    conn.execute("INSERT OR IGNORE INTO ref_dummy (id) VALUES (1)")
    conn.commit()
    conn.close()


def check_defaults(account_name, db_path, *, log_callback=None, cancel_token=None):
    """Sync defaults and retain any existing, named training records."""
    ensure_account_binding(db_path, account_name)
    credentials = fetch_credentials(account_name)
    results = fetch_and_store_reference_tables(
        account_name,
        credentials.region,
        credentials.headers,
        db_path,
        log_callback=log_callback,
        cancel_token=cancel_token,
    )
    if cancel_token and cancel_token.is_set():
        return results
    results.update(
        sync_product_reference_subset(
            account_name,
            db_path,
            reference_names=("brands", "categories"),
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
    )
    if not (cancel_token and cancel_token.is_set()):
        discovered = _discover_dummy_records(
            account_name, credentials.region, credentials.headers, db_path,
            log_callback=log_callback, cancel_token=cancel_token,
        )
        results["dummy_records"] = len(discovered)
    return results


def _search_results(url: str, headers: dict, *, log_callback=None, cancel_token=None) -> list:
    """Return rows from a Brightpearl search response, tolerating an empty search."""
    response_text, _, _ = send_request(
        url, headers, cancel_token=cancel_token, log_callback=log_callback
    )
    if not response_text:
        return []
    try:
        response = json.loads(response_text).get("response", {})
    except (json.JSONDecodeError, AttributeError):
        return []
    if isinstance(response, dict):
        rows = response.get("results", [])
        return rows if isinstance(rows, list) else []
    return response if isinstance(response, list) else []


def _discover_dummy_records(account_name, region, headers, db_path, *,
                            log_callback=None, cancel_token=None) -> dict[str, int]:
    """Find the canonical training records and copy their details into ref_dummy."""
    ensure_dummy_table(db_path)
    base = f"https://{region}.brightpearlconnect.com/public-api/{account_name}"
    found: dict[str, int] = {}

    contact_query = urlencode({"firstName": "Testy", "lastName": "McTestface", "pageSize": 100})
    for row in _search_results(
        f"{base}/contact-service/contact-search?{contact_query}", headers,
        log_callback=log_callback, cancel_token=cancel_token,
    ):
        if (isinstance(row, (list, tuple)) and len(row) > 5
                and str(row[4]).strip().casefold() == "testy"
                and str(row[5]).strip().casefold() == "mctestface"):
            _store_dummy_values(db_path, contactId=int(row[0]), firstName=row[4], lastName=row[5])
            found["contactId"] = int(row[0])
            break

    product_query = urlencode({"SKU": "CRL001", "pageSize": 100})
    for row in _search_results(
        f"{base}/product-service/product-search?{product_query}", headers,
        log_callback=log_callback, cancel_token=cancel_token,
    ):
        if (isinstance(row, (list, tuple)) and len(row) > 2
                and str(row[2]).strip().casefold() == "crl001"):
            _store_dummy_values(db_path, productId=int(row[0]), sku=row[2], productName=row[1])
            found["productId"] = int(row[0])
            break

    conn = connect_sqlite(db_path)
    try:
        method = conn.execute(
            "SELECT shippingMethodId FROM ref_shipping_methods "
            "WHERE lower(trim(name)) = lower(?) LIMIT 1",
            ("Magical shipping method",),
        ).fetchone()
    except sqlite3.OperationalError:
        method = None
    finally:
        conn.close()
    if method:
        _store_dummy_values(db_path, shippingMethodId=int(method[0]))
        found["shippingMethodId"] = int(method[0])

    labels = {"contactId": "dummy contact", "productId": "dummy product",
              "shippingMethodId": "Magical shipping method"}
    for key, record_id in found.items():
        log(f"✅ Found existing {labels[key]} ({record_id}); saved to ref_dummy.", log_callback)
    return found


def training_reference_values(account_name: str, db_path: str) -> dict[str, Any]:
    """Return the saved values displayed by the IC Training Helper.

    Missing tables and values deliberately produce blanks: the pane is also an
    at-a-glance checklist of reference data that still needs to be created.
    """
    values: dict[str, Any] = {
        "categoryId": "", "brandId": "", "defaultTaxRate": "",
        "contactId": "", "firstName": "", "lastName": "",
        "productId": "", "sku": "", "productName": "",
        "channelId": "", "statusId": "", "baseCurrencyCode": "",
        "countryIsoCode": "", "taxCode": "", "shippingNominalCode": "",
        "shippingMethodId": "",
    }
    ensure_dummy_table(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute(
                "SELECT contactId, firstName, lastName, productId, sku, productName, "
                "shippingMethodId FROM ref_dummy WHERE id = 1"
            ).fetchone()
            if row:
                values.update({key: row[key] if row[key] is not None else "" for key in row.keys()})
        except sqlite3.OperationalError:
            pass
        for table, id_column, name_column, target in (
            ("ref_product_category", "id", "name", "categoryId"),
            ("ref_brands", "brandId", "brandName", "brandId"),
        ):
            try:
                row = conn.execute(
                    f"SELECT {id_column} FROM {table} WHERE lower({name_column}) = 'other' LIMIT 1"
                ).fetchone()
                if row:
                    values[target] = row[0]
            except sqlite3.OperationalError:
                pass
        try:
            row = conn.execute("SELECT rawJson FROM ref_configuration WHERE id = 1").fetchone()
            configuration: Any = json.loads(row[0]) if row else {}
            if isinstance(configuration, dict) and isinstance(configuration.get("configuration"), dict):
                configuration = configuration["configuration"]
            if isinstance(configuration, dict):
                values["defaultTaxRate"] = configuration.get(
                    "defaultTaxRate", configuration.get("defaultTaxRateId", "")
                )
                values["channelId"] = configuration.get("defaultSalesChannelId") or 1
                values["statusId"] = configuration.get("newSalesOrderStatusId", "")
                values["baseCurrencyCode"] = configuration.get("baseCurrencyCode", "")
                values["shippingNominalCode"] = configuration.get("shippingNominalCode", "")
        except (sqlite3.OperationalError, json.JSONDecodeError, TypeError):
            pass
    finally:
        conn.close()
    try:
        region = fetch_credentials(account_name).region
    except Exception:
        region = ""
    if region == "euw1":
        values.update(countryIsoCode="GBR", taxCode="T20")
    elif region == "use1":
        values.update(countryIsoCode="USA", taxCode="T")
    return values


def inventory_reference_values(db_path: str) -> dict[str, int]:
    """Return the Inventory pane's locally synced reference-data counts."""
    tables = {
        "skus": "product_catalogue",
        "warehouses": "ref_warehouses",
        "locations": "ref_locations",
        "pricelists": "ref_price_lists",
        "pricelist_entries": "ref_price_list_values",
    }
    values = {name: 0 for name in tables}
    with sqlite3.connect(db_path) as conn:
        for name, table in tables.items():
            try:
                values[name] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                pass
    # Brightpearl accounts always include two hidden products.
    values["skus"] = max(0, values["skus"] - 2)
    return values


def allocate_random_inventory(account_name: str, db_path: str, *, price_list_id=None,
                              generic_value=20, rng=None, log_callback=None,
                              progress_callback=None, cancel_token=None) -> int:
    """Allocate every visible, stock-tracked product to non-quarantine locations."""
    credentials, base = _context(account_name, db_path)
    configuration, _ = _configuration_and_dummy(db_path)
    currency = configuration.get("baseCurrencyCode")
    if not currency:
        raise ValueError("Missing baseCurrencyCode. Run Check Defaults first.")
    try:
        generic_value = int(generic_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Generic value must be an integer.") from exc

    with sqlite3.connect(db_path) as conn:
        products = [int(row[0]) for row in conn.execute(
            "SELECT productId FROM product_catalogue "
            "WHERE productId NOT IN (1000, 1001) AND stockTracked = 1 "
            "ORDER BY productId"
        )]
        locations = [(int(row[0]), int(row[1])) for row in conn.execute(
            "SELECT locationId, warehouseId FROM ref_locations "
            "WHERE locationId IS NOT NULL AND "
            "upper(coalesce(groupingA, '') || ' ' || coalesce(groupingB, '') || ' ' || "
            "coalesce(groupingC, '') || ' ' || coalesce(groupingD, '')) NOT LIKE '%QUARANTINE%' "
            "ORDER BY warehouseId, locationId"
        )]
        prices = {}
        if price_list_id is not None:
            prices = {int(row[0]): row[1] for row in conn.execute(
                "SELECT productId, value FROM ref_price_list_values WHERE priceListId = ?",
                (int(price_list_id),),
            )}
    if not products:
        raise ValueError("No visible products are synced. Run Sync Products first.")
    if not locations:
        raise ValueError("No non-quarantine locations are synced. Run Sync Locations first.")

    randomizer = rng or random
    randomizer.shuffle(locations)
    by_warehouse: dict[int, list[dict[str, Any]]] = {}
    for index, product_id in enumerate(products):
        if cancel_token and cancel_token.is_set():
            break
        location_id, warehouse_id = locations[index % len(locations)]
        if price_list_id is not None:
            value = prices.get(product_id)
            if value is None:
                value = generic_value
        else:
            value = generic_value
        by_warehouse.setdefault(warehouse_id, []).append({
            "quantity": randomizer.randint(50, 200),
            "productId": product_id,
            "reason": "IC Training Helper random inventory",
            "locationId": location_id,
            "cost": {"currency": currency, "value": value},
        })

    allocated = 0
    batch_size = get_settings().stock_correction_batch_size
    for warehouse_id, corrections in by_warehouse.items():
        for offset in range(0, len(corrections), batch_size):
            if cancel_token and cancel_token.is_set():
                break
            batch = corrections[offset:offset + batch_size]
            _post(f"{base}/warehouse-service/warehouse/{warehouse_id}/stock-correction",
                  credentials.headers, {"corrections": batch}, log_callback=log_callback,
                  cancel_token=cancel_token, require_id=False)
            allocated += len(batch)
            if progress_callback:
                progress_callback(allocated, len(products))
    log(f"✅ Allocated random inventory for {allocated} products.", log_callback)
    return allocated


def _response_id(response: requests.Response) -> int:
    value = response.json().get("response")
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, bool):
        raise ValueError("API response did not contain a numeric ID")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("API response did not contain a numeric ID") from exc


def _post(url, headers, payload, *, log_callback=None, cancel_token=None,
          require_id: bool = True) -> Any:
    """POST JSON using the application's retry, cancellation and throttle settings."""
    retries, retry_sleep_ms = get_upload_retry_settings()
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        if cancel_token and cancel_token.is_set():
            raise RuntimeError("Operation cancelled")
        try:
            response = requests.post(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("POST", url, payload, response, log_callback)
            log(f"POST {url} attempt {attempt} → {response.status_code}", log_callback)
            response.raise_for_status()
            result = _response_id(response) if require_id else response.json().get("response")
            record_api_update(estimate_record_count(payload))
            remaining = int(response.headers.get("brightpearl-requests-remaining", 0))
            throttle_ms = int(response.headers.get("brightpearl-next-throttle-period", 0))
            if should_pause_for_throttle(remaining, throttle_ms):
                log(f"⏳ Throttling; sleeping {throttle_ms} ms.", log_callback)
                sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)
            return result
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            log(f"❌ POST attempt {attempt} failed: {exc}", log_callback)
            if attempt < retries:
                sleep_with_cancel_ms(retry_sleep_ms * attempt, cancel_token, log_callback)
    raise RuntimeError(f"POST failed after {retries} attempt(s): {last_error}")


def _configuration_and_dummy(db_path: str) -> tuple[dict[str, Any], dict[str, int]]:
    """Load the configuration document and IDs produced by Reference Data."""
    ensure_dummy_table(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        config_row = conn.execute(
            "SELECT rawJson FROM ref_configuration WHERE id = 1"
        ).fetchone()
        dummy_row = conn.execute(
            "SELECT contactId, productId, shippingMethodId FROM ref_dummy WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError("Missing defaults. Run Check Defaults and Create Ref Data first.") from exc
    finally:
        conn.close()
    if not config_row or not dummy_row:
        raise ValueError("Missing defaults. Run Check Defaults and Create Ref Data first.")
    configuration = json.loads(config_row["rawJson"])
    if isinstance(configuration, dict) and isinstance(configuration.get("configuration"), dict):
        configuration = configuration["configuration"]
    if not isinstance(configuration, dict):
        raise ValueError("ref_configuration does not contain a configuration object.")
    dummy = {key: dummy_row[key] for key in dummy_row.keys()}
    return configuration, dummy


def _paypal_payment_method_code(db_path: str, currency: str) -> str:
    """Return the active PayPal payment method for the account's base currency."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT code, name, rawJson FROM ref_payment_methods "
                "ORDER BY paymentMethodId"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError("Missing payment methods. Run Check Defaults first.") from exc

    expected_currency = str(currency).strip().casefold()
    for stored_code, stored_name, raw_json in rows:
        try:
            method = json.loads(raw_json) if raw_json else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(method, dict):
            method_currency = method.get("currency")
            if isinstance(method_currency, dict):
                method_currency = method_currency.get("code") or method_currency.get("isoCode")
            name = method.get("name") or stored_name or ""
            code = method.get("code") or stored_code
            active = method.get("active", True)
        elif isinstance(method, list):
            # The payment-method endpoint commonly returns compact rows in the
            # form [id, code, name, active, currency, bank nominal].  rawJson is
            # authoritative because older syncs stored code/name in reverse.
            code = method[1] if len(method) > 1 else stored_code
            name = method[2] if len(method) > 2 else stored_name or ""
            active = method[3] if len(method) > 3 else True
            method_currency = method[4] if len(method) > 4 else None
        else:
            continue
        if ("paypal" in str(name).casefold()
                and str(method_currency or "").strip().casefold() == expected_currency
                and active is not False
                and code):
            return str(code)
    raise ValueError(f"No active PayPal payment method found for {currency}.")


def build_bp_payment_payload(order_id: int, *, transaction_ref: str,
                             payment_method_code: str, currency: str,
                             amount_paid: Decimal, payment_date: str) -> dict[str, Any]:
    """Build the customer receipt that settles a newly created training order."""
    return {
        "transactionRef": transaction_ref,
        "transactionCode": "",
        "paymentMethodCode": payment_method_code,
        "paymentType": "RECEIPT",
        "orderId": int(order_id),
        "currencyIsoCode": currency,
        "exchangeRate": 1,
        "amountPaid": float(amount_paid),
        "paymentDate": payment_date,
        "journalRef": f"Dummy order {order_id} paid",
    }


def post_payment(base: str, headers: dict, payload: dict[str, Any], *,
                 log_callback=None, cancel_token=None) -> Any:
    """Post a customer payment using the common retried POST implementation."""
    return _post(f"{base}/accounting-service/customer-payment", headers, payload,
                 log_callback=log_callback, cancel_token=cancel_token, require_id=False)


def create_sales_order(account_name, db_path, *, log_callback=None, cancel_token=None) -> int:
    """Create and then pay the standard training sales order."""
    credentials, base = _context(account_name, db_path)
    configuration, dummy = _configuration_and_dummy(db_path)
    required = {
        "contactId": dummy.get("contactId"),
        "productId": dummy.get("productId"),
        "shippingMethodId": dummy.get("shippingMethodId"),
        "newSalesOrderStatusId": configuration.get("newSalesOrderStatusId"),
        "baseCurrencyCode": configuration.get("baseCurrencyCode"),
        "shippingNominalCode": configuration.get("shippingNominalCode"),
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise ValueError(f"Missing required reference data: {', '.join(missing)}")
    currency = str(required["baseCurrencyCode"])
    payment_method_code = _paypal_payment_method_code(db_path, currency)

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    country = "GBR" if credentials.region == "euw1" else "USA"
    tax_code = "T20" if credentials.region == "euw1" else "T"
    payload = {
        "customer": {"id": int(required["contactId"])},
        "ref": now.strftime("%Y%m%d%H%S") + f"{now.microsecond // 1000:03d}",
        "placedOn": f"{timestamp}.000+01:00",
        "taxDate": f"{timestamp}+00:00",
        "statusId": int(required["newSalesOrderStatusId"]),
        "warehouseId": 2,
        "channelId": int(configuration.get("defaultSalesChannelId") or 1),
        "priceListId": 3,
        "priceModeCode": "EXC",
        "currency": {"code": currency, "fixedExchangeRate": True,
                     "exchangeRate": "1"},
        "delivery": {
            "date": f"{timestamp}.000+01:00",
            "address": {"addressFullName": "John Doe", "companyName": "Acme",
                        "addressLine1": "123 Example Street", "addressLine2": "Suburbia",
                        "addressLine3": "Cityville", "addressLine4": "Countyshire",
                        "postalCode": "BP1 2AZ", "countryIsoCode": country,
                        "telephone": "01234 567890", "mobileTelephone": "070707070707",
                        "email": "john.doe@email.com"},
            "shippingMethodId": int(required["shippingMethodId"]),
        },
        "rows": [
            {"productId": int(required["productId"]), "name": "Creativity Lube", "quantity": "1",
             "taxCode": tax_code, "net": "10", "tax": "2", "nominalCode": "4000"},
            {"productId": 1000, "name": "Shipping", "quantity": "1", "taxCode": tax_code,
             "net": "5", "tax": "1", "nominalCode": str(required["shippingNominalCode"])},
        ],
    }
    order_id = _post(f"{base}/order-service/sales-order/", credentials.headers, payload,
                     log_callback=log_callback, cancel_token=cancel_token)
    log(f"✅ Created sales order {order_id}.", log_callback)
    amount_paid = sum(
        Decimal(str(row["quantity"])) * (Decimal(str(row["net"])) + Decimal(str(row["tax"])))
        for row in payload["rows"]
    )
    payment_payload = build_bp_payment_payload(
        order_id,
        transaction_ref=payload["ref"],
        payment_method_code=payment_method_code,
        currency=currency,
        amount_paid=amount_paid,
        payment_date=payload["placedOn"],
    )
    post_payment(base, credentials.headers, payment_payload,
                 log_callback=log_callback, cancel_token=cancel_token)
    log(f"✅ Posted payment for sales order {order_id}.", log_callback)
    return order_id


def quick_stock(account_name, db_path, *, quantity=10, log_callback=None, cancel_token=None) -> int:
    """Add the requested stock quantity to the training product in warehouse 2."""
    try:
        parsed_quantity = Decimal(str(quantity))
    except InvalidOperation as exc:
        raise ValueError("Quick Stock quantity must be a number.") from exc
    if not parsed_quantity.is_finite():
        raise ValueError("Quick Stock quantity must be a finite number.")
    normalized_quantity: int | float = (int(parsed_quantity) if parsed_quantity == parsed_quantity.to_integral()
                                        else float(parsed_quantity))
    credentials, base = _context(account_name, db_path)
    configuration, dummy = _configuration_and_dummy(db_path)
    if not dummy.get("productId") or not configuration.get("baseCurrencyCode"):
        raise ValueError("Missing productId or baseCurrencyCode. Create Ref Data first.")
    payload = {"corrections": [{"quantity": normalized_quantity,
                                "productId": int(dummy["productId"]), "reason": "Quick stock",
                                "locationId": 2, "cost": {"currency": configuration["baseCurrencyCode"],
                                                            "value": 20}}]}
    result = _post(f"{base}/warehouse-service/warehouse/2/stock-correction",
                   credentials.headers, payload, log_callback=log_callback,
                   cancel_token=cancel_token, require_id=False)
    log(f"✅ Added {normalized_quantity} units of quick stock.", log_callback)
    return result


def _context(account_name: str, db_path: str):
    ensure_account_binding(db_path, account_name)
    ensure_dummy_table(db_path)
    credentials = fetch_credentials(account_name)
    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
    return credentials, base


def _store_dummy_values(db_path: str, **values: Any) -> None:
    allowed = {"contactId", "firstName", "lastName", "productId", "sku", "productName",
               "shippingMethodId"}
    if not values or not set(values).issubset(allowed):
        raise ValueError("Unsupported dummy reference column")
    ensure_dummy_table(db_path)
    conn = connect_sqlite(db_path)
    assignments = ", ".join(f"{column} = ?" for column in values)
    conn.execute(f"UPDATE ref_dummy SET {assignments} WHERE id = 1", tuple(values.values()))
    conn.commit()
    conn.close()


def _store_id(db_path: str, column: str, value: int) -> None:
    if column not in {"contactId", "productId", "shippingMethodId"}:
        raise ValueError("Unsupported dummy reference column")
    _store_dummy_values(db_path, **{column: value})


def create_dummy_customer(account_name, db_path, *, log_callback=None, cancel_token=None) -> int:
    credentials, base = _context(account_name, db_path)
    existing = training_reference_values(account_name, db_path).get("contactId")
    if existing:
        log(f"✅ Using existing dummy customer {existing}.", log_callback)
        return int(existing)
    country = "GBR" if credentials.region == "euw1" else "USA"
    address_id = _post(
        f"{base}/contact-service/postal-address/",
        credentials.headers,
        {"addressLine1": "123 Example Street", "addressLine2": "Suburbia",
         "addressLine3": "Cityville", "addressLine4": "Countyshire",
         "postalCode": "BP1 2AZ", "countryIsoCode": country},
        log_callback=log_callback, cancel_token=cancel_token,
    )
    contact_id = _post(
        f"{base}/contact-service/contact/",
        credentials.headers,
        {"salutation": "Ms.", "firstName": "Testy", "lastName": "McTestface",
         "postAddressIds": {"DEF": address_id, "BIL": address_id, "DEL": address_id}},
        log_callback=log_callback, cancel_token=cancel_token,
    )
    _store_dummy_values(db_path, contactId=contact_id, firstName="Testy", lastName="McTestface")
    log(f"✅ Created dummy customer {contact_id}.", log_callback)
    return contact_id


def _lookup_product_defaults(db_path: str) -> tuple[int, int, str]:
    conn = sqlite3.connect(db_path)
    try:
        brand = conn.execute(
            "SELECT brandId FROM ref_brands WHERE lower(brandName) = 'other' LIMIT 1"
        ).fetchone()
        category = conn.execute(
            "SELECT id FROM ref_product_category WHERE lower(name) = 'other' LIMIT 1"
        ).fetchone()
        config_row = conn.execute("SELECT rawJson FROM ref_configuration WHERE id = 1").fetchone()
    finally:
        conn.close()
    if not brand or not category or not config_row:
        raise ValueError("Missing defaults. Run Check Defaults before creating a product.")
    configuration: Any = json.loads(config_row[0])
    if isinstance(configuration, dict) and isinstance(configuration.get("configuration"), dict):
        configuration = configuration["configuration"]
    tax_code = configuration.get("defaultTaxRateId") if isinstance(configuration, dict) else None
    if tax_code is None:
        raise ValueError("defaultTaxRateId is missing from ref_configuration.")
    return int(brand[0]), int(tax_code), str(category[0])


def create_dummy_product(account_name, db_path, *, log_callback=None, cancel_token=None) -> int:
    credentials, base = _context(account_name, db_path)
    existing = training_reference_values(account_name, db_path).get("productId")
    if existing:
        log(f"✅ Using existing dummy product {existing}.", log_callback)
        return int(existing)
    brand, tax_code, category = _lookup_product_defaults(db_path)
    payload = {
        "brandId": brand,
        "identity": {"sku": "CRL001"},
        "financialDetails": {"taxCode": {"id": tax_code}},
        "stock": {"stockTracked": True},
        "salesChannels": [{"salesChannelName": "Brightpearl", "productName": "Creativity Lube",
                           "productCondition": "new", "categories": [{"categoryCode": category}]}],
    }
    product_id = _post(f"{base}/product-service/product/", credentials.headers, payload,
                       log_callback=log_callback, cancel_token=cancel_token)
    _store_dummy_values(db_path, productId=product_id, sku="CRL001", productName="Creativity Lube")
    log(f"✅ Created dummy product {product_id}.", log_callback)
    return product_id


def create_dummy_shipping_method(account_name, db_path, *, log_callback=None, cancel_token=None) -> int:
    credentials, base = _context(account_name, db_path)
    existing = training_reference_values(account_name, db_path).get("shippingMethodId")
    if not existing:
        conn = connect_sqlite(db_path)
        try:
            row = conn.execute(
                "SELECT shippingMethodId FROM ref_shipping_methods "
                "WHERE lower(trim(name)) = lower(?) LIMIT 1",
                ("Magical shipping method",),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            conn.close()
        if row:
            existing = int(row[0])
            _store_id(db_path, "shippingMethodId", existing)
    if existing:
        log(f"✅ Using existing Magical shipping method {existing}.", log_callback)
        return int(existing)
    method_id = _post(f"{base}/warehouse-service/shipping-method", credentials.headers,
                      {"name": "Magical shipping method", "code": "MSM"},
                      log_callback=log_callback, cancel_token=cancel_token)
    _store_id(db_path, "shippingMethodId", method_id)
    log(f"✅ Created dummy shipping method {method_id}.", log_callback)
    return method_id


def do_it_all(account_name, db_path, *, log_callback=None, cancel_token=None):
    """Run every reference-data action serially, stopping on cancellation/failure."""
    results = {"references": check_defaults(account_name, db_path, log_callback=log_callback,
                                               cancel_token=cancel_token)}
    for name, action in (("contactId", create_dummy_customer), ("productId", create_dummy_product),
                         ("shippingMethodId", create_dummy_shipping_method)):
        if cancel_token and cancel_token.is_set():
            break
        results[name] = action(account_name, db_path, log_callback=log_callback,
                               cancel_token=cancel_token)
    return results
