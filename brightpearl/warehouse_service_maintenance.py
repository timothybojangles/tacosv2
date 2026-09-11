"""Warehouse Service Maintenance workflows."""
from __future__ import annotations

import csv
from csv_safety import open_csv
import json
import sqlite3
from datetime import date
from typing import Dict, List, Tuple

import requests

from .common import (
    ensure_account_binding,
    fetch_credentials_with_currency,
    get_base_currency,
    log,
    log_payload_exchange,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
    update_base_currency,
)
from .performance import estimate_record_count, record_api_update

TASK_TABLE = "warehouse_service_tasks"


def ensure_task_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (TASK_TABLE,),
    )
    exists = cur.fetchone() is not None
    if exists:
        cur.execute(f"PRAGMA table_info({TASK_TABLE})")
        columns = {row[1] for row in cur.fetchall()}
        if "originalDate" in columns or "originalRef" in columns:
            cur.execute(f"DROP TABLE IF EXISTS {TASK_TABLE}")
        elif "processedDetails" not in columns:
            cur.execute(f"ALTER TABLE {TASK_TABLE} ADD COLUMN processedDetails TEXT")

    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TASK_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            productId INTEGER NOT NULL,
            importedAt TEXT DEFAULT CURRENT_TIMESTAMP,
            processed INTEGER NOT NULL DEFAULT 0,
            processedAt TEXT,
            processedDetails TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_warehouse_options(db_path: str) -> Dict[str, int]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_warehouses (
            warehouseId INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            rawJson TEXT
        )
        """
    )
    cur.execute(
        "SELECT warehouseId, COALESCE(name, ''), COALESCE(code, '') FROM ref_warehouses ORDER BY name COLLATE NOCASE"
    )
    options: Dict[str, int] = {}
    for warehouse_id, name, code in cur.fetchall():
        label = f"{warehouse_id} - {name}" if name else str(warehouse_id)
        if code:
            label = f"{label} ({code})"
        options[label] = int(warehouse_id)
    conn.close()
    return options


def import_csv_tasks(account_name: str, db_path: str, csv_path: str, log_callback=None) -> int:
    ensure_account_binding(db_path, account_name)
    ensure_task_table(db_path)

    inserted = 0
    conn = sqlite3.connect(db_path)

    seen_product_ids = set()
    cur = conn.cursor()
    with open_csv(csv_path) as handle:
        reader = csv.DictReader(handle)
        required = {"productIds"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("CSV must contain header: productIds")
        for row in reader:
            raw_product_id = (row.get("productIds") or "").strip()
            if not raw_product_id:
                continue
            product_id = int(raw_product_id)
            if product_id in seen_product_ids:
                continue
            seen_product_ids.add(product_id)
            cur.execute(
                f"""
                INSERT INTO {TASK_TABLE} (productId, processed)
                VALUES (?, 0)
                """,
                (product_id,),
            )
            inserted += 1
    conn.commit()
    conn.close()
    log(f"✅ Imported {inserted} row(s) for Warehouse Service Maintenance.", log_callback)
    return inserted


def _request_json(method: str, url: str, headers: dict, payload=None, log_callback=None, cancel_token=None):
    if cancel_token and cancel_token.is_set():
        return None
    response = requests.request(method, url, headers=headers, json=payload, verify=False, timeout=60)
    log_payload_exchange(method, url, payload, response, log_callback)
    response.raise_for_status()
    data = response.json()
    record_api_update(estimate_record_count(payload if payload is not None else data.get("response")))

    throttle_ms = int(response.headers.get("brightpearl-next-throttle-period", 0) or 0)
    requests_remaining = int(response.headers.get("brightpearl-requests-remaining", 0) or 0)
    if should_pause_for_throttle(requests_remaining, throttle_ms):
        log(f"⏳ Throttling: waiting {throttle_ms} ms...", log_callback)
        sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)
    return data


def _get_base_currency(account_name: str, region: str, headers: dict, known: str | None, log_callback=None, cancel_token=None) -> str:
    if known:
        return known
    config_url = (
        f"https://{region}.brightpearlconnect.com/public-api/{account_name}/"
        "integration-service/account-configuration"
    )
    currency = get_base_currency(config_url, headers, log_callback, cancel_token)
    if currency:
        update_base_currency(account_name, currency)
        return currency
    raise ValueError("Unable to determine base currency for this account.")


def process_tasks(
    account_name: str,
    db_path: str,
    warehouse_id: int,
    log_callback=None,
    cancel_token=None,
    single_correction_per_call: bool = False,
) -> Tuple[int, int]:
    ensure_account_binding(db_path, account_name)
    ensure_task_table(db_path)

    credentials, stored_currency = fetch_credentials_with_currency(account_name)
    currency = _get_base_currency(
        account_name,
        credentials.region,
        credentials.headers,
        stored_currency,
        log_callback=log_callback,
        cancel_token=cancel_token,
    )
    today = date.today().isoformat()
    base_url = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, productId FROM {TASK_TABLE} WHERE processed = 0 ORDER BY id"
    )
    pending_rows = cur.fetchall()
    total = len(pending_rows)
    if total == 0:
        conn.close()
        log("⚠️ No unprocessed warehouse service rows found.", log_callback)
        return 0, 0

    processed = 0
    for index, (task_id, product_id) in enumerate(pending_rows, start=1):
        if cancel_token and cancel_token.is_set():
            break
        log(f"🔄 Processing productId {product_id} ({index}/{total})", log_callback)

        availability_url = (
            f"{base_url}/warehouse-service/product-availability/{product_id}/"
            "?includeOptional=breakDownByLocation"
        )
        availability_data = _request_json("GET", availability_url, credentials.headers, log_callback=log_callback, cancel_token=cancel_token)
        response_map = (availability_data or {}).get("response", {})
        product_key = str(product_id)
        warehouse_map = ((response_map.get(product_key) or {}).get("warehouses") or {}).get(str(warehouse_id), {})
        by_location = warehouse_map.get("byLocation") or {}

        remove_corrections: List[dict] = []
        for location_key, values in by_location.items():
            on_hand = int((values or {}).get("onHand") or 0)
            if on_hand <= 0:
                continue
            remove_corrections.append(
                {
                    "quantity": -on_hand,
                    "productId": int(product_id),
                    "reason": f"Performance Correction {today}",
                    "locationId": int(location_key),
                }
            )

        if not remove_corrections:
            cur.execute(
                f"UPDATE {TASK_TABLE} SET processed = 1, processedAt = CURRENT_TIMESTAMP, processedDetails = ? WHERE id = ?",
                (f"No onHand stock in warehouse {warehouse_id}", task_id),
            )
            conn.commit()
            processed += 1
            progress_pct = int((processed / total) * 100)
            log(f"PROGRESS:{progress_pct}", log_callback)
            continue

        post_url = f"{base_url}/warehouse-service/warehouse/{warehouse_id}/stock-correction"
        correction_refs: List[int] = []
        if single_correction_per_call:
            for correction in remove_corrections:
                remove_response = _request_json(
                    "POST",
                    post_url,
                    credentials.headers,
                    payload={"corrections": [correction]},
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
                correction_refs.extend((remove_response or {}).get("response") or [])
        else:
            remove_payload = {"corrections": remove_corrections}
            remove_response = _request_json(
                "POST",
                post_url,
                credentials.headers,
                payload=remove_payload,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            correction_refs = (remove_response or {}).get("response") or []

        restore_quantities: Dict[Tuple[int, int, float], int] = {}
        for correction_ref in correction_refs:
            correction_url = f"{base_url}/warehouse-service/warehouse/{warehouse_id}/stock-correction/{correction_ref}"
            correction_data = _request_json("GET", correction_url, credentials.headers, log_callback=log_callback, cancel_token=cancel_token)
            for note in (correction_data or {}).get("response", []):
                for goods_moved in note.get("goodsMoved", []):
                    p_id = int(goods_moved.get("productId") or 0)
                    location_id = int(goods_moved.get("destinationLocationId") or 0)
                    quantity = int(goods_moved.get("quantity") or 0)
                    value = float(((goods_moved.get("productValue") or {}).get("value")) or 0)
                    if p_id and location_id and quantity:
                        key = (p_id, location_id, value)
                        restore_quantities[key] = restore_quantities.get(key, 0) + abs(quantity)

        restore_corrections: List[dict] = []
        if restore_quantities:
            for (p_id, location_id, value), quantity in restore_quantities.items():
                restore_corrections.append(
                    {
                        "quantity": quantity,
                        "productId": p_id,
                        "reason": f"Performance Correction {today}",
                        "locationId": location_id,
                        "cost": {
                            "currency": currency,
                            "value": value,
                        },
                    }
                )
        else:
            # Fallback when correction lookups do not return goodsMoved rows.
            for correction in remove_corrections:
                restore_corrections.append(
                    {
                        "quantity": abs(int(correction["quantity"])),
                        "productId": int(correction["productId"]),
                        "reason": f"Performance Correction {today}",
                        "locationId": int(correction["locationId"]),
                        "cost": {
                            "currency": currency,
                            "value": 0,
                        },
                    }
                )

        if single_correction_per_call:
            for correction in restore_corrections:
                _request_json(
                    "POST",
                    post_url,
                    credentials.headers,
                    payload={"corrections": [correction]},
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
        else:
            restore_payload = {"corrections": restore_corrections}
            _request_json(
                "POST",
                post_url,
                credentials.headers,
                payload=restore_payload,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )

        cur.execute(
            f"UPDATE {TASK_TABLE} SET processed = 1, processedAt = CURRENT_TIMESTAMP, processedDetails = ? WHERE id = ?",
            (f"Processed warehouse {warehouse_id}", task_id),
        )
        conn.commit()

        processed += 1
        progress_pct = int((processed / total) * 100)
        log(f"PROGRESS:{progress_pct}", log_callback)

    conn.close()
    return processed, total
