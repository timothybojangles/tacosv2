# utils/sync_historic_orders.py

import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from brightpearl.common import (
    connect_sqlite,
    ensure_processing_columns,
    mark_rows_processed,
    unprocessed_where_clause,
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    log as bp_log,
    sleep_with_cancel_ms,
    log_payload_exchange,
)
from brightpearl.performance import estimate_record_count, record_api_update
from brightpearl.settings import get_settings, get_upload_retry_settings

urllib3.disable_warnings(InsecureRequestWarning)

FAILED_OUT_DIR = "output"

ORDERS_TABLE = "validated_historic_orders"
ROWS_TABLE = "validated_historic_order_rows"

LogCallback = Optional[Callable[[str], None]]

_LOG_CALLBACK: LogCallback = None
_PROGRESS_CALLBACK: Optional[Callable[[int, int], None]] = None


def _log(message: str) -> None:
    """Proxy log that mirrors to the shared Brightpearl logger and GUI callback."""
    bp_log(f"[Historic Sync] {message}", _LOG_CALLBACK)


def _sleep_with_log(ms: int) -> None:
    _log(f"⏳ Sleeping for {ms} ms due to throttling…")
    sleep_with_cancel_ms(ms)

def load_validated_orders(db_path):
    """
    Loads all validated historic orders + their rows from SQLite.
    Matches the exact schema from validator_historic_orders.py.
    """
    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    ensure_processing_columns(conn, ORDERS_TABLE)
    cur.execute(f"""
        SELECT
            id, order_ref, placed_on, tax_date, delivery_date, currency, exchange_rate,
            contactId, channelId, warehouseId, statusId, priceListId, shippingMethodId,
            delivery_address_full_name, delivery_address_company_name,
            delivery_address_line1, delivery_address_line2, delivery_address_line3, delivery_address_line4,
            delivery_address_post_code, delivery_address_countryIsoCode,
            delivery_telephone, delivery_email,
            orderId, source_csv_headers, source_csv_filename
        FROM {ORDERS_TABLE}
        WHERE {unprocessed_where_clause()}
    """)
    orders = []
    for row in cur.fetchall():
        orders.append({
            "id": row[0],
            "order_ref": row[1],
            "placed_on": row[2],
            "tax_date": row[3],
            "delivery_date": row[4],
            "currency": row[5],
            "exchange_rate": row[6],
            "contactId": row[7],
            "channelId": row[8],
            "warehouseId": row[9],
            "statusId": row[10],
            "priceListId": row[11],
            "shippingMethodId": row[12],
            "del_full": row[13],
            "del_company": row[14],
            "del1": row[15],
            "del2": row[16],
            "del3": row[17],
            "del4": row[18],
            "del_pc": row[19],
            "del_countryIso": row[20],
            "del_tel": row[21],
            "del_email": row[22],
            "orderId": row[23],
            "source_headers": json.loads(row[24]) if row[24] else [],
            "source_filename": row[25]
        })

    # Attach rows per order_ref
    for o in orders:
        cur.execute(f"""
            SELECT line_number, sku, quantity, row_net, row_tax, tax_code, nominal_code,
                   item_name, productId, rowType, original_row_json
            FROM {ROWS_TABLE}
            WHERE order_ref = ?
            ORDER BY line_number
        """, (o["order_ref"],))
        o["rows"] = [{
            "line_number": r[0],
            "sku": r[1],
            "quantity": r[2],
            "row_net": r[3],
            "row_tax": r[4],
            "tax_code": r[5],
            "nominal_code": r[6],
            "item_name": r[7],
            "productId": r[8],
            "rowType": r[9],
            "original_row_json": r[10],
        } for r in cur.fetchall()]

    conn.close()
    return orders

def update_order_id(db_path, order_ref, order_id):
    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    cur.execute(f"UPDATE {ORDERS_TABLE} SET orderId = ? WHERE order_ref = ?", (order_id, order_ref))
    conn.commit()
    conn.close()

def update_progress(current, total):
    if _PROGRESS_CALLBACK:
        _PROGRESS_CALLBACK(current, total)
        return

    pct = int((current / max(total, 1)) * 100)
    sys.stdout.write(f"\rProgress: {pct}%")
    sys.stdout.flush()
    print(f"\nPROGRESS:{pct}", flush=True)

def _iso_datetime_from_date(datestr):
    """Convert 'YYYY-MM-DD' or ISO-like string to full ISO with timezone (UTC if none)."""
    s = (datestr or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        pass
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None

def _strip_nones(obj):
    if isinstance(obj, dict):
        return {k: _strip_nones(v) for k, v in obj.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [ _strip_nones(v) for v in obj if v not in (None, "", [], {}) ]
    return obj

def build_order_payload(order):
    """
    Build the historic order payload for POST /order-service/order
    Only include non-null fields; keep to API shape you provided.
    """
    # Delivery block
    delivery_obj = {}
    if order.get("delivery_date"):
        delivery_obj["deliveryDate"] = _iso_datetime_from_date(order["delivery_date"])
    if order.get("shippingMethodId"):
        delivery_obj["shippingMethodId"] = int(order["shippingMethodId"])

    # Parties.delivery address
    parties_delivery = {}
    if order.get("del_full"):    parties_delivery["addressFullName"] = order["del_full"]
    if order.get("del_company"): parties_delivery["companyName"] = order["del_company"]
    if order.get("del1"):        parties_delivery["addressLine1"] = order["del1"]
    if order.get("del2"):        parties_delivery["addressLine2"] = order["del2"]
    if order.get("del3"):        parties_delivery["addressLine3"] = order["del3"]
    if order.get("del4"):        parties_delivery["addressLine4"] = order["del4"]
    if order.get("del_pc"):      parties_delivery["postalCode"] = order["del_pc"]
    if order.get("del_countryIso"): parties_delivery["countryIsoCode"] = order["del_countryIso"]
    if order.get("del_tel"):     parties_delivery["telephone"] = order["del_tel"]
    if order.get("del_email"):   parties_delivery["email"] = order["del_email"].lower()

    payload = {
        "orderTypeCode": "SO",
        "historicalOrder": True,
        "reference": order["order_ref"],
        "placedOn": _iso_datetime_from_date(order["placed_on"]),
        "orderStatus": {"orderStatusId": int(order["statusId"])},
        "currency": {"orderCurrencyCode": order["currency"]},
        "parties": {
            "customer": {"contactId": int(order["contactId"])}
        }
    }

    # Optional top-levels per stored refs
    if order.get("priceListId"):
        payload["priceListId"] = int(order["priceListId"])
    if delivery_obj:
        payload["delivery"] = delivery_obj
    if parties_delivery:
        payload.setdefault("parties", {})["delivery"] = parties_delivery
    # Optional: include channel/warehouse if supported by your tenant
    if order.get("channelId"):
        payload["channelId"] = int(order["channelId"])
    if order.get("warehouseId"):
        payload["warehouseId"] = int(order["warehouseId"])
    # Optional: include taxDate if you use it and endpoint allows it
    if order.get("tax_date"):
        td = _iso_datetime_from_date(order["tax_date"])
        if td:
            payload["taxDate"] = td

    return _strip_nones(payload)

def post_order(url, headers, payload):
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, verify=False)
            log_payload_exchange("POST", url, payload, resp, _LOG_CALLBACK)
            _log(f"ORDER POST attempt {attempt} → {resp.status_code}")
            if resp.status_code == 200:
                record_api_update(estimate_record_count(payload))
                data = {}
                ct = (resp.headers.get("Content-Type") or "").lower()
                if "json" in ct:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {}
                created_id = None
                try:
                    # expected: {"response": 123456}
                    created_id = int(data.get("response"))
                except Exception:
                    pass

                rem = int(resp.headers.get("brightpearl-requests-remaining", 3))
                thr = int(resp.headers.get("brightpearl-next-throttle-period", 0))
                if rem <= get_settings().throttle_threshold and thr > 0:
                    _sleep_with_log(thr)
                else:
                    sleep_with_cancel_ms(default_sleep_ms)

                if created_id:
                    return True, created_id
                else:
                    _log(f"⚠️ 200 OK but no order id in body: {str(data)[:300]}")
            else:
                _log(f"❌ Order {resp.status_code}: {resp.text[:500]}")
        except requests.RequestException as e:
            _log(f"❌ Order request error: {e}")
        sleep_with_cancel_ms(default_sleep_ms * attempt)
    return False, None

def _to_int_magnitude(v):
    # strict: only accept whole numbers (e.g., "1", "1.0"); error if 1.5
    try:
        d = Decimal(str(v))
        if d != d.to_integral_value():
            raise ValueError(f"Quantity must be an integer, got {v!r}")
        return int(d)
    except (InvalidOperation, ValueError):
        # fallback: try plain int conversion; will raise if bad
        return int(float(v))

def build_row_payload(row):
    qty_int = _to_int_magnitude(row["quantity"])
    payload = {
        "quantity": {"magnitude": qty_int},     # <-- now guaranteed int
        "rowValue": {
            "taxCode": row["tax_code"],
            "rowNet": {"value": float(row["row_net"])},
            "rowTax": {"value": float(row["row_tax"])}
        }
    }
    if row.get("productId"):
        payload["productId"] = int(row["productId"])
    if row.get("nominal_code"):
        payload["nominalCode"] = row["nominal_code"]
    if row.get("item_name"):
        payload["name"] = row["item_name"]
    return _strip_nones(payload)

def post_row(url, headers, payload):
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, verify=False)
            log_payload_exchange("POST", url, payload, resp, _LOG_CALLBACK)
            _log(f"ROW POST attempt {attempt} → {resp.status_code}")
            if resp.status_code == 200:
                record_api_update(estimate_record_count(payload))
                rem = int(resp.headers.get("brightpearl-requests-remaining", 3))
                thr = int(resp.headers.get("brightpearl-next-throttle-period", 0))
                if rem <= get_settings().throttle_threshold and thr > 0:
                    _sleep_with_log(thr)
                else:
                    sleep_with_cancel_ms(default_sleep_ms)
                return True, None
            else:
                _log(f"❌ Row {resp.status_code}: {resp.text[:500]}")
        except requests.RequestException as e:
            _log(f"❌ Row request error: {e}")
        sleep_with_cancel_ms(default_sleep_ms * attempt)
    return False, None

def write_failed_csv(account_name, failed_groups, filename_tag):
    """
    failed_groups is a list of {"order_ref": ..., "rows": [rowdicts_with_original_row_json,...]}
    or for order-level failures, a list of order dicts, each having "rows".
    """
    if not failed_groups:
        return
    # Determine headers from the first group if available, else union all keys
    headers = []
    for g in failed_groups:
        rows = g.get("rows", [])
        for r in rows:
            try:
                headers = list(json.loads(r["original_row_json"]).keys())
                break
            except Exception:
                continue
        if headers:
            break
    if not headers:
        # Fallback: union of keys
        keys = set()
        for g in failed_groups:
            for r in g.get("rows", []):
                try:
                    keys.update(json.loads(r["original_row_json"]).keys())
                except Exception:
                    pass
        headers = list(keys)

    out_path = os.path.join(FAILED_OUT_DIR, f"{account_name}_{filename_tag}_{int(time.time())}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        total = 0
        for g in failed_groups:
            for r in g.get("rows", []):
                try:
                    w.writerow(json.loads(r["original_row_json"]))
                    total += 1
                except Exception:
                    pass
    _log(f"⚠️ Wrote {total} rows to {out_path}")

def main(account_name, db_path, *, log_callback: LogCallback = None, progress_callback: Optional[Callable[[int, int], None]] = None):
    global _LOG_CALLBACK, _PROGRESS_CALLBACK
    _LOG_CALLBACK = log_callback
    _PROGRESS_CALLBACK = progress_callback

    ensure_account_binding(db_path, account_name)

    credentials: Credentials = fetch_credentials(account_name)
    orders = load_validated_orders(db_path)
    total = len(orders)
    done = 0
    _log(f"Found {total} validated historic orders.")
    if not _PROGRESS_CALLBACK:
        print("PROGRESS:0", flush=True)

    headers = {
        "brightpearl-app-ref": credentials.app_ref,
        "brightpearl-account-token": credentials.token,
        "Content-Type": "application/json"
    }

    ORDER_URL_TMPL = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/order-service/order"
    ROW_URL_TMPL   = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/order-service/order/{{order_id}}/row"

    failed_order_creates = []  # list of order dicts (each includes .rows)
    failed_row_posts = []      # list of {"order_ref": ..., "rows": [failed_rows]}

    for order in orders:
        # 1) Create order
        order_payload = build_order_payload(order)
        ok, created_id = post_order(ORDER_URL_TMPL, headers, order_payload)
        if not ok or not created_id:
            # keep all original rows for export
            failed_order_creates.append(order)
            done += 1
            update_progress(done, total)
            continue

        # Save returned orderId in DB for audit/retry
        try:
            update_order_id(db_path, order["order_ref"], created_id)
        except Exception as e:
            _log(f"⚠️ Failed to update orderId in DB for {order['order_ref']}: {e}")

        # 2) Post rows
        row_url = ROW_URL_TMPL.format(order_id=created_id)
        per_order_failed_rows = []
        for r in order["rows"]:
            payload = build_row_payload(r)
            ok_row, _ = post_row(row_url, headers, payload)
            if not ok_row:
                per_order_failed_rows.append(r)

        if per_order_failed_rows:
            failed_row_posts.append({"order_ref": order["order_ref"], "rows": per_order_failed_rows})
        else:
            conn = connect_sqlite(db_path)
            try:
                mark_rows_processed(conn, ORDERS_TABLE, order["id"], f"Created historic order {created_id}")
                conn.commit()
            finally:
                conn.close()

        done += 1
        update_progress(done, total)

    print()  # newline after progress

    if failed_order_creates:
        # Transform to groups compatible with write_failed_csv
        groups = [{"order_ref": o["order_ref"], "rows": o["rows"]} for o in failed_order_creates]
        write_failed_csv(account_name, groups, "failed_historic_orders")
    if failed_row_posts:
        write_failed_csv(account_name, failed_row_posts, "failed_historic_order_rows")

    if failed_order_creates or failed_row_posts:
        _log(f"❌ Orders failed: {len(failed_order_creates)} | Orders with row failures: {len(failed_row_posts)}")
    else:
        _log("✅ All historic orders and rows synced successfully.")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python sync_historic_orders.py <account_name> <db_path>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
