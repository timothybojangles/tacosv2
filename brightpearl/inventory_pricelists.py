"""Inventory import helpers for syncing and applying Brightpearl price lists."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from typing import Dict, List, Tuple

import requests

from brightpearl.common import (
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    log,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)
from brightpearl.performance import estimate_record_count, record_api_update
from brightpearl.settings import get_settings
from reference_data import fetch_and_store_reference_tables


def _ensure_price_list_values_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_price_list_values (
            productId INTEGER NOT NULL,
            priceListId INTEGER NOT NULL,
            value REAL,
            currencyCode TEXT,
            currencyId INTEGER,
            rawJson TEXT,
            PRIMARY KEY (productId, priceListId)
        )
        """
    )
    conn.commit()


def _extract_unit_price(quantity_price) -> float | None:
    if not isinstance(quantity_price, dict) or not quantity_price:
        return None
    raw = quantity_price.get("1")
    if raw is None:
        for key, value in quantity_price.items():
            if key is None:
                continue
            try:
                if int(str(key).strip()) == 1:
                    raw = value
                    break
            except (TypeError, ValueError):
                continue
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def sync_inventory_pricelists(account_name: str, db_path: str, log_callback=None, cancel_token=None) -> Dict[str, int]:
    """Sync ref_price_lists and all product-price values into local DB."""
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return {}

    results = fetch_and_store_reference_tables(
        account_name,
        credentials.region,
        credentials.headers,
        db_path,
        reference_keys=("price_lists",),
        log_callback=log_callback,
        cancel_token=cancel_token,
    )

    if cancel_token and cancel_token.is_set():
        return results

    options_url = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
        "/product-service/product-price/"
    )

    try:
        options_response = requests.options(
            options_url,
            headers=credentials.headers,
            verify=False,
            timeout=60,
        )
        options_response.raise_for_status()
    except requests.RequestException as exc:
        log(f"❌ Failed to fetch product-price OPTIONS: {exc}", log_callback)
        return results

    try:
        options_payload = options_response.json().get("response", {})
    except json.JSONDecodeError as exc:
        log(f"❌ Could not parse OPTIONS response JSON: {exc}", log_callback)
        return results

    uris = options_payload.get("getUris") or []
    if not isinstance(uris, list):
        uris = []

    conn = sqlite3.connect(db_path)
    _ensure_price_list_values_table(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM ref_price_list_values")

    inserted = 0
    total_uris = max(1, len(uris))

    for idx, uri in enumerate(uris, start=1):
        if cancel_token and cancel_token.is_set():
            log("🛑 Pricelist sync cancelled.", log_callback)
            break

        uri_path = str(uri or "").strip()
        if not uri_path:
            continue
        if not uri_path.startswith("/"):
            uri_path = f"/{uri_path}"
        if not uri_path.startswith("/product-service/"):
            uri_path = f"/product-service{uri_path}"

        url = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}{uri_path}"
        try:
            response = requests.get(url, headers=credentials.headers, verify=False, timeout=60)
            response.raise_for_status()
            next_throttle_period = int(response.headers.get("brightpearl-next-throttle-period", 0))
            requests_remaining = int(response.headers.get("brightpearl-requests-remaining", 0))
            payload = response.json().get("response", [])
        except (requests.RequestException, ValueError) as exc:
            log(f"⚠️ Failed to fetch {uri_path}: {exc}", log_callback)
            continue

        record_api_update(estimate_record_count(payload))

        for product_row in payload:
            if not isinstance(product_row, dict):
                continue
            product_id = product_row.get("productId")
            if product_id is None:
                continue
            price_lists = product_row.get("priceLists") or []
            for row in price_lists:
                if not isinstance(row, dict):
                    continue
                price_list_id = row.get("priceListId")
                if price_list_id is None:
                    continue
                unit_price = _extract_unit_price(row.get("quantityPrice"))
                cur.execute(
                    """
                    INSERT OR REPLACE INTO ref_price_list_values
                    (productId, priceListId, value, currencyCode, currencyId, rawJson)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(product_id),
                        int(price_list_id),
                        unit_price,
                        row.get("currencyCode"),
                        row.get("currencyId"),
                        json.dumps(row, separators=(",", ":")),
                    ),
                )
                inserted += 1

        progress = int((idx / total_uris) * 100)
        log(f"📦 Synced {idx}/{len(uris)} pricelist chunks (PROGRESS:{progress})", log_callback)

        if should_pause_for_throttle(requests_remaining, next_throttle_period):
            log(f"⏳ Throttling: waiting {next_throttle_period} ms...", log_callback)
            sleep_with_cancel_ms(next_throttle_period, cancel_token, log_callback)

    conn.commit()
    conn.close()
    results["price_list_values"] = inserted
    log(f"✅ Stored {inserted} product pricelist rows.", log_callback)
    return results


def get_ref_price_lists(db_path: str) -> List[Tuple[int, str]]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT priceListId, COALESCE(name, code, CAST(priceListId AS TEXT)) FROM ref_price_lists ORDER BY name")
    rows = [(int(row[0]), str(row[1])) for row in cur.fetchall()]
    conn.close()
    return rows


def set_inventory_costs_from_pricelist(account_name: str, db_path: str, price_list_id: int, log_callback=None) -> Dict[str, object]:
    ensure_account_binding(db_path, account_name)
    conn = sqlite3.connect(db_path)
    _ensure_price_list_values_table(conn)
    cur = conn.cursor()

    cur.execute("SELECT productId, sku FROM validated_inventory")
    inventory_rows = cur.fetchall()

    updated = 0
    missing_rows: List[dict] = []
    zero_rows: List[dict] = []

    for product_id, sku in inventory_rows:
        cur.execute(
            "SELECT value FROM ref_price_list_values WHERE productId = ? AND priceListId = ?",
            (product_id, price_list_id),
        )
        price_row = cur.fetchone()
        if not price_row or price_row[0] is None:
            missing_rows.append({"productId": product_id, "sku": sku})
            continue

        try:
            value = float(price_row[0])
        except (TypeError, ValueError):
            missing_rows.append({"productId": product_id, "sku": sku})
            continue

        if value == 0.0:
            zero_rows.append({"productId": product_id, "sku": sku})

        cur.execute(
            "UPDATE validated_inventory SET costprice = ? WHERE productId = ?",
            (value, product_id),
        )
        updated += cur.rowcount

    conn.commit()
    conn.close()

    out_dir = get_settings().unmatched_output_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    timestamp = int(time.time())
    missing_path = ""
    zero_path = ""

    if missing_rows:
        missing_path = os.path.abspath(os.path.join(out_dir, f"{account_name}_missing_pricelist_values_{timestamp}.csv"))
        with open(missing_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["productId", "sku"])
            writer.writeheader()
            writer.writerows(missing_rows)
        log(f"⚠️ Missing pricelist value export: {missing_path}", log_callback)

    if zero_rows:
        zero_path = os.path.abspath(os.path.join(out_dir, f"{account_name}_zero_pricelist_values_{timestamp}.csv"))
        with open(zero_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["productId", "sku"])
            writer.writeheader()
            writer.writerows(zero_rows)
        log(f"⚠️ Zero pricelist value export: {zero_path}", log_callback)

    log(f"✅ Updated costprice on {updated} validated inventory rows using pricelist {price_list_id}.", log_callback)
    return {
        "updated": updated,
        "missing_count": len(missing_rows),
        "zero_count": len(zero_rows),
        "missing_path": missing_path,
        "zero_path": zero_path,
    }
