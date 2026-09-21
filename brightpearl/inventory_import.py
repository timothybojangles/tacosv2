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
from .settings import get_upload_retry_settings


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

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    product_columns = """
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
    """
    cursor.execute(f"CREATE TABLE IF NOT EXISTS product_catalogue({product_columns})")
    cursor.execute("DROP TABLE IF EXISTS product_catalogue_sync")
    cursor.execute(f"CREATE TABLE product_catalogue_sync({product_columns})")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reference_sync_progress (
            reference_name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            completed INTEGER NOT NULL,
            total INTEGER,
            message TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        INSERT INTO reference_sync_progress(reference_name, status, completed, total, message, updated_at)
        VALUES ('products', 'running', 0, NULL, 'Starting product catalogue sync', CURRENT_TIMESTAMP)
        ON CONFLICT(reference_name) DO UPDATE SET status='running', completed=0, total=NULL,
            message=excluded.message, updated_at=CURRENT_TIMESTAMP
    """)
    conn.commit()
    insert_sql = """
        INSERT OR REPLACE INTO product_catalogue_sync (
            productId, productName, SKU, barcode, EAN, UPC, ISBN, MPN,
            stockTracked, salesChannelName, createdOn, updatedOn,
            brightpearlCategoryCode, productGroupId, brandId, productTypeId,
            productStatus, primarySupplierId
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    first_result = 1
    product_counter = 0
    expected_count = None
    try:
        while True:
            if cancel_token and cancel_token.is_set():
                raise RuntimeError(f"Product catalogue sync cancelled after {product_counter} products.")
            paginated_url = f"{api_url_catalogue}?pageSize=500&firstResult={first_result}"
            max_retries, retry_sleep_ms = get_upload_retry_settings()
            json_response = None
            next_throttle_period = 0
            requests_remaining = 0
            for attempt in range(1, max_retries + 1):
                json_response, next_throttle_period, requests_remaining = send_request(
                    paginated_url, credentials.headers, cancel_token=cancel_token, log_callback=log_callback
                )
                if json_response:
                    break
                if attempt < max_retries:
                    delay_ms = retry_sleep_ms * attempt
                    log(
                        f"Product page at firstResult={first_result} failed; "
                        f"retrying in {delay_ms} ms ({attempt}/{max_retries}).",
                        log_callback,
                    )
                    sleep_with_cancel_ms(delay_ms, cancel_token, log_callback)
            if not json_response:
                raise RuntimeError(
                    f"Product catalogue request failed at firstResult={first_result}; "
                    f"the previous complete catalogue was preserved."
                )
            try:
                data = json.loads(json_response)["response"]
                results = data["results"]
                metadata = data["metaData"]
                available = int(metadata["resultsAvailable"])
                last_result = int(metadata["lastResult"])
                more_pages_available = bool(metadata["morePagesAvailable"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("Brightpearl returned invalid product-search pagination metadata.") from exc
            if expected_count is None:
                expected_count = available
            elif available != expected_count:
                raise RuntimeError("Brightpearl product count changed during sync; retry for a consistent snapshot.")
            if results and last_result < first_result:
                raise RuntimeError("Brightpearl product-search pagination did not advance.")
            products = _flatten_product_rows(results)
            cursor.executemany(insert_sql, [
                (
                    product.get("productId"), product.get("productName"), product.get("SKU"),
                    product.get("barcode"), product.get("EAN"), product.get("UPC"),
                    product.get("ISBN"), product.get("MPN"), int(product.get("stockTracked", False)),
                    product.get("salesChannelName"), product.get("createdOn"), product.get("updatedOn"),
                    product.get("brightpearlCategoryCode"), product.get("productGroupId"),
                    product.get("brandId"), product.get("productTypeId"),
                    product.get("productStatus"), product.get("primarySupplierId"),
                ) for product in products.values()
            ])
            product_counter += len(results)
            cursor.execute("""
                UPDATE reference_sync_progress
                SET completed=?, total=?, message=?, updated_at=CURRENT_TIMESTAMP
                WHERE reference_name='products'
            """, (product_counter, expected_count, f"Fetched through Brightpearl result {last_result}"))
            conn.commit()
            record_api_update(len(results))
            log(f"📦 Synced {product_counter} of {expected_count} products", log_callback)
            log_search_progress(metadata, log_callback)
            if should_pause_for_throttle(requests_remaining, next_throttle_period):
                log(f"⏳ Throttling: waiting {next_throttle_period} ms...", log_callback)
                sleep_with_cancel_ms(next_throttle_period, cancel_token, log_callback)
            if not more_pages_available:
                break
            first_result = last_result + 1

        staged_count = cursor.execute("SELECT COUNT(*) FROM product_catalogue_sync").fetchone()[0]
        if expected_count is None or staged_count != expected_count or product_counter != expected_count:
            raise RuntimeError(
                f"Incomplete product catalogue: Brightpearl reported {expected_count}, "
                f"received {product_counter}, and staged {staged_count} unique products. "
                "The previous complete catalogue was preserved."
            )
        conn.commit()
        cursor.execute("BEGIN")
        cursor.execute("DELETE FROM product_catalogue")
        cursor.execute("INSERT INTO product_catalogue SELECT * FROM product_catalogue_sync")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_product_catalogue_sku ON product_catalogue(SKU)")
        cursor.execute("DROP TABLE product_catalogue_sync")
        cursor.execute("""
            UPDATE reference_sync_progress
            SET status='succeeded', completed=?, total=?, message='Complete', updated_at=CURRENT_TIMESTAMP
            WHERE reference_name='products'
        """, (staged_count, staged_count))
        conn.commit()
        log(f"PROGRESS:100", log_callback)
        return staged_count
    except Exception as exc:
        conn.rollback()
        cursor.execute("""
            UPDATE reference_sync_progress
            SET status='failed', completed=?, total=?, message=?, updated_at=CURRENT_TIMESTAMP
            WHERE reference_name='products'
        """, (product_counter, expected_count, str(exc)))
        conn.commit()
        raise
    finally:
        conn.close()
