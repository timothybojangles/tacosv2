import csv
import re
import time
import sqlite3
import sys
import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning
from brightpearl.common import (
    connect_sqlite,
    ensure_processing_columns,
    SYNC_DEBUG_LOG,
    ensure_account_binding,
    fetch_credentials_with_currency,
    log_sync,
    log_payload_exchange,
    mark_rows_processed,
    unprocessed_where_clause,
)
from brightpearl.performance import record_api_update
from brightpearl.settings import get_settings, get_upload_retry_settings
from brightpearl.throttle import parse_int_header, sleep_with_log, throttle_decision
urllib3.disable_warnings(InsecureRequestWarning)

TABLE_NAME = "validated_inventory"
def load_validated_inventory(db_path):
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    ensure_processing_columns(conn, TABLE_NAME)
    cursor.execute(f"SELECT id, sku, quantity, locationId, costprice, warehouseId, productId FROM {TABLE_NAME} WHERE {unprocessed_where_clause()}")
    records = cursor.fetchall()
    conn.close()

    inventory = []
    for row in records:
        row_id, sku, quantity, locationId, costprice, warehouseId, productId = row
        inventory.append({
            "id": row_id,
            "sku": sku,
            "quantity": quantity,
            "locationId": locationId,
            "costprice": costprice,
            "warehouseId": warehouseId,
            "productId": productId
        })
    return inventory

def update_progress(current, total):
    progress = current / max(total, 1)
    bar_length = 40
    block = int(round(bar_length * progress))
    text = f"\rProgress: [{'#' * block + '-' * (bar_length - block)}] {int(progress * 100)}%"
    sys.stdout.write(text)
    sys.stdout.flush()
    print(f"\nPROGRESS:{int(progress * 100)}", flush=True)

def _extract_error_product_id(message):
    """Return the productId referenced in a Brightpearl error message, if present."""
    if not message:
        return None
    product_match = re.search(r"\bproduct\s+([A-Za-z0-9_-]{4,})\b", str(message), re.IGNORECASE)
    if product_match:
        return product_match.group(1)
    fallback_match = re.search(r"\b([0-9]{4,})\b", str(message))
    if fallback_match:
        return fallback_match.group(1)
    return None


def _extract_error_messages_by_product_id(response):
    """Map productIds in a failed POST response to their Brightpearl error messages."""
    try:
        response_json = response.json()
    except ValueError:
        return {}

    if not isinstance(response_json, dict):
        return {}

    messages_by_product_id = {}
    for error in response_json.get("errors", []):
        if not isinstance(error, dict):
            continue
        message = error.get("message") or ""
        product_id = _extract_error_product_id(message)
        if not product_id:
            continue
        messages_by_product_id.setdefault(str(product_id), []).append(message)

    return {
        product_id: " | ".join(messages)
        for product_id, messages in messages_by_product_id.items()
    }


def _failed_response(error_messages_by_product_id=None):
    return {
        "ok": False,
        "error_messages_by_product_id": error_messages_by_product_id or {},
    }


def _successful_response():
    return {"ok": True, "error_messages_by_product_id": {}}


def send_batch(payload, region, account_name, wh_id, app_ref, token,
               completed_batches, total_batches, log_callback=None, cancel_token=None):
    if cancel_token and cancel_token.is_set():
        return _failed_response()

    url = f"https://{region}.brightpearlconnect.com/public-api/{account_name}/warehouse-service/warehouse/{wh_id}/stock-correction"
    headers = {
        "brightpearl-app-ref": app_ref,
        "brightpearl-account-token": token,
        "Content-Type": "application/json"
    }

    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            return _failed_response()
        try:
            response = requests.post(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("POST", url, payload, response, log_callback)
            log_sync(f"🔁 Attempt {attempt}: Status {response.status_code}", log_callback)

            if response.status_code == 200:
                record_api_update(len(payload.get("corrections", [])))
                # Read headers (safe parsing + defaults)
                requests_remaining = parse_int_header(response.headers, "brightpearl-requests-remaining", 2)
                throttle_ms = parse_int_header(response.headers, "brightpearl-next-throttle-period", 0)

                # Always log the headers so we know what's happening
                log_sync(f"🔎 Rate-limit headers: requests_remaining={requests_remaining}, next_throttle_ms={throttle_ms}", log_callback)

                progress_percent = int((completed_batches / max(total_batches, 1)) * 100)
                log_sync(f"***SUCCESS*** ({progress_percent}% complete)", log_callback)

                # Decide how long to sleep and why, then sleep (cancellable)
                sleep_ms, reason = throttle_decision(
                    requests_remaining,
                    throttle_ms,
                    threshold=get_settings().throttle_threshold,
                )
                if sleep_ms > 0:
                    sleep_with_log(
                        sleep_ms,
                        log_callback,
                        cancel_token=cancel_token,
                        reason=reason,
                        log_file=SYNC_DEBUG_LOG,
                    )
                return _successful_response()
            else:
                log_sync(f"❌ Attempt {attempt}: Server responded with status {response.status_code}", log_callback)
                try:
                    log_sync(f"📥 Response body:\n{response.text}", log_callback)
                except Exception as e:
                    log_sync(f"⚠️ Could not read response body: {e}", log_callback)

                if response.status_code == 409:
                    error_messages = _extract_error_messages_by_product_id(response)
                    if error_messages:
                        log_sync(
                            f"⚠️ 409 conflict details found for productIds: {', '.join(sorted(error_messages))}",
                            log_callback,
                        )
                    return _failed_response(error_messages)
        except requests.RequestException as e:
            log_sync(f"❌ Attempt {attempt}: Request error: {e}", log_callback)

        # Exponential-ish backoff between retries (cancellable)
        sleep_with_log(
            default_sleep_ms * attempt,
            log_callback,
            cancel_token=cancel_token,
            reason="retry backoff",
            log_file=SYNC_DEBUG_LOG,
        )

    return _failed_response()

def write_failed_batches(failed_rows, headers, account_name, log_callback=None):
    filename = f"{account_name}_failed_sync_batches_{int(time.time())}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(failed_rows)
    log_sync(f"⚠️ {len(failed_rows)} rows written to {filename} due to failed sync.", log_callback)


def _to_float_or_none(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _failed_row(item, error_messages_by_product_id=None):
    product_id = item.get("productId")
    return {
        "sku": item.get("sku"),
        "productId": product_id,
        "quantity": item.get("quantity"),
        "locationId": item.get("locationId"),
        "costprice": item.get("costprice"),
        "warehouseId": item.get("warehouseId"),
        "message": (error_messages_by_product_id or {}).get(str(product_id), ""),
    }


def main(
    account_name,
    db_path,
    progress_callback=None,
    log_callback=None,
    cancel_token=None,
    allow_zero_blanks=True,
):
    ensure_account_binding(db_path, account_name)
    credentials, base_currency = fetch_credentials_with_currency(account_name)
    app_ref, token, region = credentials.app_ref, credentials.token, credentials.region
    inventory = load_validated_inventory(db_path)
    batch_size = get_settings().stock_correction_batch_size

    failed_rows = []
    headers = ["sku", "productId", "quantity", "locationId", "costprice", "warehouseId", "message"]

    # group inventory by warehouse
    inventory_by_warehouse = {}
    for item in inventory:
        inventory_by_warehouse.setdefault(item["warehouseId"], []).append(item)

    total_batches = sum((len(items) + batch_size - 1) // batch_size for items in inventory_by_warehouse.values())
    completed_batches = 0

    for wh_id, items in inventory_by_warehouse.items():
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before processing more warehouses.", log_callback)
            # mark all remaining items as failed
            for it in items:
                failed_rows.append(_failed_row(it))
            continue

        for i in range(0, len(items), batch_size):
            if cancel_token and cancel_token.is_set():
                log_sync("🛑 Cancel before next batch.", log_callback)
                # all remaining items in this warehouse -> failed
                for it in items[i:]:
                    failed_rows.append(_failed_row(it))
                break

            batch = items[i:i+batch_size]
            corrections = []
            for item in batch:
                quantity = _to_float_or_none(item.get("quantity"))
                cost_value = _to_float_or_none(item.get("costprice"))
                if allow_zero_blanks:
                    if quantity is None:
                        quantity = 0.0
                    if cost_value is None:
                        cost_value = 0.0
                else:
                    if quantity is None or quantity == 0.0:
                        continue
                    if cost_value is None or cost_value == 0.0:
                        continue

                corrections.append(
                    {
                        "productId": int(item["productId"]),
                        "quantity": float(quantity),
                        "locationId": int(item["locationId"]),
                        "cost": {"currency": base_currency, "value": float(cost_value)},
                        "reason": "Stock Sync",
                    }
                )

            if not corrections:
                completed_batches += 1
                if progress_callback:
                    progress_callback(completed_batches, total_batches)
                continue

            payload = {"accountName": account_name, "warehouseId": wh_id, "corrections": corrections}
            log_sync(f"\n🔄 Syncing batch of {len(batch)} to WH {wh_id}...", log_callback)

            result = send_batch(payload, region, account_name, wh_id, app_ref, token,
                                completed_batches + 1, total_batches,
                                log_callback=log_callback, cancel_token=cancel_token)
            if result["ok"]:
                conn = connect_sqlite(db_path)
                try:
                    mark_rows_processed(conn, TABLE_NAME, [item["id"] for item in batch], f"Synced stock correction batch to warehouse {wh_id}")
                    conn.commit()
                finally:
                    conn.close()
            else:
                error_messages = result.get("error_messages_by_product_id", {})
                for item in batch:
                    failed_rows.append(_failed_row(item, error_messages))

            completed_batches += 1
            if progress_callback:
                progress_callback(completed_batches, total_batches)

        # outer loop continues to next warehouse; cancellation may have added rows to failed_rows above

    log_sync(f"\n✅ Completed {completed_batches}/{total_batches} batches (attempted).", log_callback)

    if failed_rows:
        write_failed_batches(failed_rows, headers, account_name, log_callback)
        if cancel_token and cancel_token.is_set():
            log_sync("❌ Cancelled: wrote pending rows to failed CSV for retry.", log_callback)
        else:
            log_sync("❌ Some batches failed. See CSV for retry.", log_callback)
    else:
        if cancel_token and cancel_token.is_set():
            log_sync("ℹ️ Cancelled with no pending rows to retry.", log_callback)
        else:
            log_sync("✅ All batches synced successfully.", log_callback)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python sync.py <account_name> <db_path>")
        sys.exit(1)

    account_name = sys.argv[1]
    db_path = sys.argv[2]
    main(account_name, db_path)
