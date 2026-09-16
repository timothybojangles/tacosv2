"""Inventory import API helpers."""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, Iterable

from .common import (
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    get_base_currency,
    log,
    log_search_progress,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
    update_base_currency,
)
from .performance import record_api_update


def _flatten_product_rows(rows: Iterable[Iterable]) -> Dict[int, dict]:
    element_titles = [
        "productId",
        "productName",
        "SKU",
        "barcode",
        "EAN",
        "UPC",
        "ISBN",
        "MPN",
        "stockTracked",
        "salesChannelName",
        "createdOn",
        "updatedOn",
        "brightpearlCategoryCode",
        "productGroupId",
        "brandId",
        "productTypeId",
        "productStatus",
        "primarySupplierId",
    ]
    catalogue: Dict[int, dict] = {}
    for row in rows:
        item = {element_titles[i]: row[i] for i in range(len(row))}
        product_id = item.get("productId")
        if product_id is not None:
            catalogue[int(product_id)] = item
    return catalogue


def update_product_catalogue(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Fetch the product catalogue from Brightpearl and persist it locally."""
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    api_url_catalogue = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "product-service/product-search"
    )
    api_url_config = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "integration-service/account-configuration"
    )

    base_currency = get_base_currency(api_url_config, credentials.headers, log_callback, cancel_token)
    if base_currency:
        update_base_currency(account_name, base_currency)
        log(f"✅ baseCurrencyCode '{base_currency}' saved for {account_name}", log_callback)
    else:
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancelled before/while fetching base currency.", log_callback)
            return 0
        log("⚠️ Failed to fetch baseCurrencyCode", log_callback)

    first_result = 1
    more_pages_available = True
    all_products = []
    product_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after syncing {product_counter} products (so far).", log_callback)
            break

        paginated_url = f"{api_url_catalogue}?pageSize=500&firstResult={first_result}"
        json_response, next_throttle_period, requests_remaining = send_request(
            paginated_url, credentials.headers, cancel_token=cancel_token, log_callback=log_callback
        )

        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after request (so far: {product_counter}).", log_callback)
            break

        if json_response:
            data = json.loads(json_response).get("response", {})
            results = data.get("results", [])
            all_products.extend(results)
            product_counter += len(results)
            record_api_update(len(results))
            log(f"📦 Synced {product_counter} products", log_callback)

            metadata = data.get("metaData", {})
            log_search_progress(metadata, log_callback)
            more_pages_available = metadata.get("morePagesAvailable", False)
            last_result = metadata.get("lastResult", first_result + 500)
            first_result = last_result + 1

            if should_pause_for_throttle(requests_remaining, next_throttle_period):
                log(f"⏳ Throttling: waiting {next_throttle_period} ms...", log_callback)
                sleep_with_cancel_ms(next_throttle_period, cancel_token, log_callback)
        else:
            more_pages_available = False

    if not all_products:
        if cancel_token and cancel_token.is_set():
            return product_counter
        log("⚠️ No products returned from Brightpearl.", log_callback)
        return product_counter

    if cancel_token and cancel_token.is_set():
        return product_counter

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS product_catalogue(
            productId INTEGER PRIMARY KEY,
            productName TEXT,
            SKU TEXT,
            barcode TEXT,
            EAN TEXT,
            UPC TEXT,
            ISBN TEXT,
            MPN TEXT,
            stockTracked INTEGER,
            salesChannelName TEXT,
            createdOn TEXT,
            updatedOn TEXT,
            brightpearlCategoryCode TEXT,
            productGroupId INTEGER,
            brandId INTEGER,
            productTypeId INTEGER,
            productStatus TEXT,
            primarySupplierId INTEGER
        )
        """
    )
    cursor.execute("DELETE FROM product_catalogue")

    product_catalogue = _flatten_product_rows(all_products)
    for product in product_catalogue.values():
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancelled during DB write; partial commit.", log_callback)
            break
        cursor.execute(
            """
            INSERT INTO product_catalogue (
                productId,
                productName,
                SKU,
                barcode,
                EAN,
                UPC,
                ISBN,
                MPN,
                stockTracked,
                salesChannelName,
                createdOn,
                updatedOn,
                brightpearlCategoryCode,
                productGroupId,
                brandId,
                productTypeId,
                productStatus,
                primarySupplierId
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                product.get("productId"),
                product.get("productName"),
                product.get("SKU"),
                product.get("barcode"),
                product.get("EAN"),
                product.get("UPC"),
                product.get("ISBN"),
                product.get("MPN"),
                int(product.get("stockTracked", False)),
                product.get("salesChannelName"),
                product.get("createdOn"),
                product.get("updatedOn"),
                product.get("brightpearlCategoryCode"),
                product.get("productGroupId"),
                product.get("brandId"),
                product.get("productTypeId"),
                product.get("productStatus"),
                product.get("primarySupplierId"),
            ),
        )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_catalogue_sku ON product_catalogue(SKU)"
    )
    conn.commit()
    conn.close()
    return product_counter
