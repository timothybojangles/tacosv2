import csv
import json
import os
import sqlite3
import sys
import time
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

ORDERS_TABLE = "validated_sales_orders"
ROWS_TABLE = "validated_sales_order_rows"

LogCallback = Optional[Callable[[str], None]]

_LOG_CALLBACK: LogCallback = None
_PROGRESS_CALLBACK: Optional[Callable[[int, int], None]] = None


def _log(message: str) -> None:
    bp_log(f"[Sales Sync] {message}", _LOG_CALLBACK)


def _sleep_with_log(ms: int, *, cancel_token=None) -> None:
    _log(f"⏳ Sleeping for {ms} ms due to throttling…")
    sleep_with_cancel_ms(ms, cancel_token=cancel_token, log_callback=_LOG_CALLBACK)


def load_validated_orders(db_path):
    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    ensure_processing_columns(conn, ORDERS_TABLE)
    cur.execute(f"""
        SELECT
            id, order_ref, contactId, placed_on, tax_date, delivery_date, warehouseId, channelId, statusId,
            currency, priceListId, exchangeRate,
            COALESCE(shippingMethodId, ''), payment_amount, payment_date, payment_ref, payment_method_code, orderId,
            delivery_name, delivery_line1, delivery_line2, delivery_line3, delivery_line4,
            delivery_postcode, delivery_country, delivery_countryIsoCode, delivery_telephone, delivery_email,
            source_csv_headers, source_csv_filename
        FROM {ORDERS_TABLE}
        WHERE {unprocessed_where_clause()}
    """)
    orders = []
    for row in cur.fetchall():
        orders.append({
            "id": row[0],
            "order_ref": row[1],
            "contactId": row[2],
            "placed_on": row[3],
            "tax_date": row[4],
            "delivery_date": row[5],
            "warehouseId": row[6],
            "channelId": row[7],
            "statusId": row[8],
            "currency": row[9],
            "priceListId": row[10],
            "exchangeRate": row[11],
            "shippingMethodId": row[12] if row[12] != "" else None,

            # payment fields
            "payment_amount": row[13],
            "payment_date": row[14],         # YYYY-MM-DD
            "payment_ref": row[15],
            "payment_method_code": row[16],
            "orderId": row[17],              # may be None

            # delivery fields
            "del_name": row[18],
            "del1": row[19],
            "del2": row[20],
            "del3": row[21],
            "del4": row[22],
            "del_pc": row[23],
            "del_country": row[24],
            "del_countryIso": row[25],
            "del_tel": row[26],
            "del_email": row[27],

            # source
            "source_headers": json.loads(row[28]) if row[28] else [],
            "source_filename": row[29]
        })

    # attach rows
    for o in orders:
        cur.execute(f"""
            SELECT line_number, item_name, item_sku, item_qty, item_tax_code, row_net, row_tax_amount, productId, rowType, original_row_json
            FROM {ROWS_TABLE}
            WHERE order_ref = ?
            ORDER BY line_number
        """, (o["order_ref"],))
        o["rows"] = [{
            "line_number": r[0],
            "item_name": r[1],
            "item_sku": r[2],
            "item_qty": r[3],
            "item_tax_code": r[4],
            "row_net": r[5],
            "row_tax_amount": r[6],
            "productId": r[7],
            "rowType": r[8],
            "original_row_json": r[9],
        } for r in cur.fetchall()]
    conn.close()
    return orders

def update_order_id(db_path, order_ref, order_id):
    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE validated_sales_orders SET orderId = ? WHERE order_ref = ?", (order_id, order_ref))
    conn.commit()
    conn.close()

def update_progress(current, total):
    if _PROGRESS_CALLBACK:
        _PROGRESS_CALLBACK(current, total)
        return

    progress = current / max(total, 1)
    bar_length = 40
    block = int(round(bar_length * progress))
    text = f"\rProgress: [{'#' * block + '-' * (bar_length - block)}] {int(progress * 100)}%"
    sys.stdout.write(text)
    sys.stdout.flush()
    # Emit a machine-readable line for the CLI fallback
    print(f"\nPROGRESS:{int(progress * 100)}", flush=True)

def build_bp_order_payload(order):
    """
    Builds a Brightpearl Sales Order payload shaped like the provided example.

    Input 'order' is the dict assembled in load_validated_orders(), e.g.:
      - order_ref, contactId, placed_on (YYYY-MM-DD), warehouseId, channelId,
        currency, priceListId, exchangeRate, shippingMethodId (optional),
        delivery fields, and order["rows"] (from validated_sales_order_rows).

    Notes:
      • placedOn/taxDate/delivery.date use provided values when present; otherwise
        they are generated as ISO datetimes from the placed_on date.
      • priceModeCode is set to "INC" (tax-inclusive), adjust if you need "EXC".
      • statusId is INCLUDED ONLY if you add order["statusId"] before calling this;
        otherwise it’s omitted safely.
      • nominalCode defaults to "4000" if you don’t store it per product.
      • Country uses 3-letter ISO if we can convert common 2-letter values.
    """
    from datetime import datetime, timezone

    def iso_datetime_from_date(datestr):
        # If you only have a YYYY-MM-DD date, send midnight UTC with offset +00:00
        # You can upgrade this to include local time if you later store it.
        if not datestr:
            return None
        try:
            dt = datetime.fromisoformat(datestr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            # Fallback: treat as date only
            try:
                dt = datetime.strptime(datestr, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                return datestr  # last resort: pass through

    def iso3(country_iso2_or_name):
        s = (country_iso2_or_name or "").strip().upper()
        # minimal mapper; extend as required
        m = {"GB": "GBR", "UK": "GBR", "US": "USA", "IE": "IRL"}
        if s in m: return m[s]
        if len(s) == 3: return s
        return s  # pass through (API may still accept)

    def strip_nones(d):
        # Remove keys where value is None or empty string
        return {k: v for k, v in d.items() if v is not None and v != ""}

    # Build rows (use strings for quantity/net/tax to mirror your example)
    bp_rows = []
    for r in order["rows"]:
        is_product = (r.get("rowType") == "PRODUCT" and r.get("productId"))
        line = {
            # Include productId only for product rows
            "productId": int(r["productId"]) if is_product else None,
            "name": r.get("item_name") or ("Charge" if not is_product else ""),
            "quantity": str(r.get("item_qty") or "1"),
            "taxCode": r.get("item_tax_code") or "",
            "net": str(r.get("row_net") or "0"),
            "tax": str(r.get("row_tax_amount") or "0"),
            "nominalCode": "4000",       # Optional: swap to per-product if you store it
            "externalRef": ""
        }
        bp_rows.append(strip_nones(line))

    placed_iso = iso_datetime_from_date(order["placed_on"])
    # If you want a different tax date (e.g., “now”), change this:
    tax_iso = iso_datetime_from_date(order.get("tax_date") or order["placed_on"])
    delivery_iso = iso_datetime_from_date(order.get("delivery_date") or order["placed_on"])

    payload = {
        "customer": {"id": int(order["contactId"])},
        "ref": order["order_ref"],
        "placedOn": placed_iso,
        "taxDate": tax_iso,
        "parentId": None,               # Supply if you later support parent orders
        "statusId": order.get("statusId"),  # Optional; omitted if not present
        "warehouseId": int(order["warehouseId"]),
        "staffOwnerId": None,           # Map from ref_users_staff if/when you add it
        "channelId": int(order["channelId"]),
        "externalRef": None,
        "installedIntegrationInstanceId": None,
        "leadSourceId": None,
        "teamId": None,
        "priceListId": int(order["priceListId"]),
        "priceModeCode": "INC",         # or "EXC" if you send net prices
        "currency": {
            "code": order["currency"],
            "fixedExchangeRate": True,
            "exchangeRate": str(order.get("exchangeRate", 1))
        },
        "delivery": {
            "date": delivery_iso,
            "address": strip_nones({
                "addressFullName": order.get("del_name"),
                "companyName": None,  # Add if you capture it
                "addressLine1": order.get("del1"),
                "addressLine2": order.get("del2"),
                "addressLine3": order.get("del3"),
                "addressLine4": order.get("del4"),
                "postalCode": order.get("del_pc"),
                "countryIsoCode": iso3(order.get("del_countryIso") or order.get("del_country")),
                "telephone": order.get("del_tel"),
                "mobileTelephone": None,  # Add if you capture it
                "email": (order.get("del_email") or "").lower() if order.get("del_email") else None
            }),
            "shippingMethodId": int(order["shippingMethodId"]) if order.get("shippingMethodId") else None
        },
        "rows": bp_rows
    }

    # Clean up optional None values at the top level
    payload["delivery"] = strip_nones(payload["delivery"])
    return strip_nones(payload)

def post_order(url, headers, payload, *, cancel_token=None):
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            _log("🛑 Cancel requested before posting order.")
            return False, None
        try:
            resp = requests.post(url, json=payload, headers=headers, verify=False)
            log_payload_exchange("POST", url, payload, resp, _LOG_CALLBACK)
            _log(f"ORDER POST {url} attempt {attempt} → {resp.status_code}")
            if 200 <= resp.status_code < 300:
                record_api_update(estimate_record_count(payload))
                data = resp.json() if "application/json" in resp.headers.get("Content-Type","") else {}
                created_id = None
                try:
                    created_id = data.get("response")
                except Exception:
                    pass
                rem = int(resp.headers.get("brightpearl-requests-remaining", 3))
                thr = int(resp.headers.get("brightpearl-next-throttle-period", 0))
                if rem <= get_settings().throttle_threshold and thr > 0:
                    _sleep_with_log(thr, cancel_token=cancel_token)
                else:
                    sleep_with_cancel_ms(
                        default_sleep_ms,
                        cancel_token=cancel_token,
                        log_callback=_LOG_CALLBACK,
                    )
                if created_id:
                    return True, int(created_id)
                else:
                    _log("⚠️ 200 OK but no 'response' id in body.")
            else:
                _log(f"❌ Order {resp.status_code}: {resp.text[:500]}")
        except requests.RequestException as e:
            _log(f"❌ Order request error: {e}")
        sleep_with_cancel_ms(
            int(attempt * 1000),
            cancel_token=cancel_token,
            log_callback=_LOG_CALLBACK,
        )
    return False, None

def build_bp_payment_payload(order_id, order):
    """
    Build the payment payload per your spec.
    Only called if payment_amount > 0.
    """
    return {
        "transactionRef": order.get("payment_ref") or "",
        "transactionCode": "",
        "paymentMethodCode": order.get("payment_method_code"),
        "paymentType": "RECEIPT",
        "orderId": int(order_id),
        "currencyIsoCode": order["currency"],           # e.g. GBP
        "exchangeRate": float(order.get("exchangeRate", 1) or 1),
        "amountPaid": float(order.get("payment_amount", 0) or 0),
        "paymentDate": order.get("payment_date"),       # YYYY-MM-DD
        "journalRef": f"Online order {order_id} paid"
    }

def post_payment(url, headers, payload, *, cancel_token=None):
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            _log("🛑 Cancel requested before posting payment.")
            return False, None
        try:
            resp = requests.post(url, json=payload, headers=headers, verify=False)
            log_payload_exchange("POST", url, payload, resp, _LOG_CALLBACK)
            _log(f"PAYMENT POST {url} attempt {attempt} → {resp.status_code}")
            if resp.status_code == 200:
                record_api_update(estimate_record_count(payload))
                rem = int(resp.headers.get("brightpearl-requests-remaining", 3))
                thr = int(resp.headers.get("brightpearl-next-throttle-period", 0))
                if rem <= get_settings().throttle_threshold and thr > 0:
                    _sleep_with_log(thr, cancel_token=cancel_token)
                else:
                    sleep_with_cancel_ms(
                        default_sleep_ms,
                        cancel_token=cancel_token,
                        log_callback=_LOG_CALLBACK,
                    )
                return True, (resp.json() if "application/json" in resp.headers.get("Content-Type","") else resp.text)
            else:
                _log(f"❌ Payment {resp.status_code}: {resp.text[:500]}")
        except requests.RequestException as e:
            _log(f"❌ Payment request error: {e}")
        sleep_with_cancel_ms(
            int(attempt * 1000),
            cancel_token=cancel_token,
            log_callback=_LOG_CALLBACK,
        )
    return False, None

def write_failed_orders_csv(account_name, failed, filename_tag):
    if not failed:
        return
    headers = set()
    for fo in failed:
        for r in fo["rows"]:
            headers.update(json.loads(r["original_row_json"]).keys())
    headers = list(headers)
    out_path = os.path.join(FAILED_OUT_DIR, f"{account_name}_{filename_tag}_{int(time.time())}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for fo in failed:
            for r in fo["rows"]:
                w.writerow(json.loads(r["original_row_json"]))
    _log(f"⚠️ Wrote {sum(len(o['rows']) for o in failed)} rows to {out_path}")

def main(
    account_name,
    db_path,
    *,
    log_callback: LogCallback = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_token=None,
):
    global _LOG_CALLBACK, _PROGRESS_CALLBACK
    _LOG_CALLBACK = log_callback
    _PROGRESS_CALLBACK = progress_callback

    ensure_account_binding(db_path, account_name)

    credentials: Credentials = fetch_credentials(account_name)
    orders = load_validated_orders(db_path)
    total = len(orders)
    done = 0
    _log(f"Found {total} validated sales orders to sync.")
    if not _PROGRESS_CALLBACK:
        print("PROGRESS:0", flush=True)

    headers = {
        "brightpearl-app-ref": credentials.app_ref,
        "brightpearl-account-token": credentials.token,
        "Content-Type": "application/json"
    }

    PAYMENT_URL = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "accounting-service/customer-payment"
    )
    ORDER_CREATE_URL = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "order-service/sales-order"
    )

    failed_orders = []
    failed_payments = []

    for order in orders:
        if cancel_token and cancel_token.is_set():
            _log("🛑 Cancel requested before processing next order.")
            break
        created_id = order.get("orderId")
        if created_id:
            _log(f"↪️ Reusing saved Brightpearl orderId {created_id} for {order['order_ref']}.")
        else:
            # 1) Create order
            order_payload = build_bp_order_payload(order)
            ok, created_id = post_order(
                ORDER_CREATE_URL,
                headers,
                order_payload,
                cancel_token=cancel_token,
            )
            if cancel_token and cancel_token.is_set():
                _log("🛑 Cancel detected after order attempt; stopping.")
                break
            if not ok or not created_id:
                failed_orders.append(order)
                done += 1
                update_progress(done, total)
                continue

            # store orderId for future reference before any later step can fail
            try:
                update_order_id(db_path, order["order_ref"], created_id)
                order["orderId"] = created_id
            except Exception as e:
                _log(f"⚠️ Failed to update orderId in DB for {order['order_ref']}: {e}")

        # 2) Conditionally post payment
        try:
            amt = float(order.get("payment_amount") or 0)
        except Exception:
            amt = 0.0

        if amt > 0 and order.get("payment_method_code") and order.get("payment_date"):
            payment_payload = build_bp_payment_payload(created_id, order)
            okp, _ = post_payment(
                PAYMENT_URL,
                headers,
                payment_payload,
                cancel_token=cancel_token,
            )
            if cancel_token and cancel_token.is_set():
                _log("🛑 Cancel detected after payment attempt; stopping.")
                break
            if not okp:
                failed_payments.append(order)
        elif amt > 0:
            _log(f"⚠️ Skipping payment for {order['order_ref']}: missing payment_method_code or payment_date")

        if not (order in failed_payments):
            conn = connect_sqlite(db_path)
            try:
                mark_rows_processed(conn, ORDERS_TABLE, order["id"], f"Created sales order {created_id}")
                conn.commit()
            finally:
                conn.close()
        done += 1
        update_progress(done, total)

    print()

    if failed_orders:
        write_failed_orders_csv(account_name, failed_orders, "failed_sales_orders")
    if failed_payments:
        write_failed_orders_csv(account_name, failed_payments, "failed_sales_order_payments")

    if cancel_token and cancel_token.is_set():
        _log("🛑 Sales order sync cancelled by user.")
    elif failed_orders or failed_payments:
        _log(f"❌ Orders failed: {len(failed_orders)} | Payments failed: {len(failed_payments)}")
    else:
        _log("✅ All orders (and payments, where applicable) synced successfully.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python sync_sales_orders.py <account_name> <db_path>")
        sys.exit(1)

    main(sys.argv[1], sys.argv[2])
