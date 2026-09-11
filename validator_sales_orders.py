import csv
from csv_safety import open_csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from tkinter import Tk, filedialog
from typing import Callable, Optional

from brightpearl.common import processing_column_definitions, connect_sqlite, ensure_account_binding, log as bp_log
from brightpearl.settings import get_settings
from row_validation import product_field_length_errors, row_with_validation_error

UNMATCHED_DIR = "output/unmatched"

ORDERS_TABLE = "validated_sales_orders"
ROWS_TABLE = "validated_sales_order_rows"

TEMPLATE_HEADERS = [
    "order_ref",
    "placed_on",
    "tax_date",
    "delivery_date",
    "customer_email",
    "warehouse",
    "channel",
    "order_status",
    "currency",
    "price_list",
    "exchange_rate",
    "shipping_method",
    "payment_amount",
    "payment_date",
    "payment_ref",
    "payment_method_code",
    "delivery_address_name",
    "delivery_address_line1",
    "delivery_address_line2",
    "delivery_address_line3",
    "delivery_address_line4",
    "delivery_postcode",
    "delivery_country",
    "delivery_telephone",
    "delivery_email",
    "item_name",
    "item_sku",
    "item qty",
    "item_tax_code",
    "row_net",
    "row_tax_amount",
]

LogCallback = Optional[Callable[[str], None]]

_LOG_CALLBACK: LogCallback = None


def log(msg: str):
    bp_log(f"[Sales Validation] {msg}", _LOG_CALLBACK)

def select_csv_file():
    root = Tk(); root.withdraw()
    return filedialog.askopenfilename(
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Select CSV file for Sales Orders validation"
    )

def _unmatched_dir() -> str:
    return get_settings().unmatched_output_dir or UNMATCHED_DIR


def ensure_tables(conn: sqlite3.Connection):
    cur = conn.cursor()
    # Orders (one row per order_ref)
    cur.execute(f"""
        DROP TABLE IF EXISTS {ORDERS_TABLE}
    """)
    cur.execute(f"""
        CREATE TABLE {ORDERS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_ref TEXT UNIQUE NOT NULL,
            contactId INTEGER NOT NULL,
            placed_on TEXT NOT NULL,
            tax_date TEXT,
            delivery_date TEXT,
            warehouseId INTEGER NOT NULL,
            channelId INTEGER NOT NULL,
            statusId INTEGER NOT NULL,
            currency TEXT NOT NULL,
            priceListId INTEGER NOT NULL,
            exchangeRate REAL NOT NULL,
            shippingMethodId INTEGER,
            payment_amount REAL,
            payment_date TEXT,            -- YYYY-MM-DD
            payment_ref TEXT,
            payment_method_code TEXT,
            orderId INTEGER,
            delivery_name TEXT,
            delivery_line1 TEXT,
            delivery_line2 TEXT,
            delivery_line3 TEXT,
            delivery_line4 TEXT,
            delivery_postcode TEXT,
            delivery_country TEXT,
            delivery_countryIsoCode TEXT,
            delivery_telephone TEXT,
            delivery_email TEXT,
            source_csv_headers TEXT,
            source_csv_filename TEXT{processing_column_definitions()}
        )
    """)
    # Order rows (one row per CSV line)
    cur.execute(f"""
        DROP TABLE IF EXISTS {ROWS_TABLE}
    """)
    cur.execute(f"""
        CREATE TABLE {ROWS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_ref TEXT NOT NULL,
            line_number INTEGER NOT NULL,
            item_name TEXT,
            item_sku TEXT,
            item_qty REAL,
            item_tax_code TEXT,
            row_net REAL,
            row_tax_amount REAL,
            productId INTEGER,
            rowType TEXT,              -- 'PRODUCT' or 'NOMINAL'
            original_row_json TEXT
        )
    """)
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{ROWS_TABLE}_ref ON {ROWS_TABLE}(order_ref)")
    conn.commit()

def _safe_float(v, default=0.0):
    try:
        s = (v or "").strip()
        s = s.replace(",", "")  # guard against 1,234.56
        return float(s)
    except Exception:
        return default


def _canonicalize_header(name: str) -> str:
    s = (name or "").strip().lstrip("\ufeff").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _row_get(row: dict, field_name: str, default=""):
    target = _canonicalize_header(field_name)
    for key, value in row.items():
        if _canonicalize_header(str(key)) == target:
            return value
    return default


def _try_parse_float(v):
    s = (v or "").strip()
    if s == "":
        return None, None
    try:
        return float(s.replace(",", "")), None
    except Exception:
        return None, f"invalid numeric value '{s}'"

def _parse_date_dmy(s):
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return None

def _parse_date_or_datetime(s):
    """
    Accept either a date (dd/mm/yy, dd/mm/yyyy, yyyy-mm-dd) or an ISO datetime string.
    Returns an ISO 8601 string (date-only as YYYY-MM-DD or datetime with offset) or None.
    """
    s = (s or "").strip()
    if not s:
        return None

    # First try standard date formats
    parsed_date = _parse_date_dmy(s)
    if parsed_date:
        return parsed_date

    # Try ISO datetime or date
    try:
        dt = datetime.fromisoformat(s)
        return dt.isoformat()
    except Exception:
        return None

def _maybe_json(s):
    s = (s or "").strip()
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            return s
    return s

def _matches_name_or_code(cur, table, code_col, name_col, needle: str):
    """Case-insensitive exact match on code OR name (name may be JSON)."""
    if not needle:
        return None
    n = needle.strip().lower()

    # Try code match
    cur.execute(f"SELECT rowid, * FROM {table} WHERE LOWER({code_col}) = ?", (n,))
    row = cur.fetchone()
    if row:
        return row

    # Try name match (name may be JSON with i18n)
    cur.execute(f"SELECT rowid, {name_col} FROM {table}")
    for r in cur.fetchall():
        name_val = _maybe_json(r[1])
        if isinstance(name_val, dict):
            # Try common keys
            for k in ("en", "EN", "default", "name"):
                if k in name_val and isinstance(name_val[k], str) and name_val[k].strip().lower() == n:
                    return r
        elif isinstance(name_val, str):
            if name_val.strip().lower() == n:
                return r
    return None

def _warehouse_to_id(cur, warehouse_field: str):
    s = (warehouse_field or "").strip()
    if not s:
        return None

    if s.isdigit():
        cur.execute("SELECT warehouseId FROM ref_warehouses WHERE warehouseId = ?", (int(s),))
        row = cur.fetchone()
        return int(row[0]) if row else None

    cur.execute("SELECT warehouseId FROM ref_warehouses WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))", (s,))
    row = cur.fetchone()
    if row:
        return int(row[0])

    m = re.match(r"^(\d+)\b", s)
    if m:
        cur.execute("SELECT warehouseId FROM ref_warehouses WHERE warehouseId = ?", (int(m.group(1)),))
        row = cur.fetchone()
        return int(row[0]) if row else None

    return None

def _country_to_iso(alpha2_or_name: str):
    s = (alpha2_or_name or "").strip().upper()
    # Simple, pragmatic mapper—extend as needed
    if s in ("UK", "GB", "GBR", "UNITED KINGDOM"): return "GB"
    if len(s) == 2: return s
    return None

def _status_to_id(cur, status_field: str, required_order_type: str = "SO"):
    """
    Accepts status by id (e.g. '35'), code (e.g. 'NEW'), or name (string / i18n JSON).
    Resolves only statuses whose rawJson.orderTypeCode == required_order_type.
    """
    s = (status_field or "").strip()
    if not s:
        return None

    # If numeric, direct id check
    if s.isdigit():
        cur.execute("SELECT statusId, rawJson FROM ref_order_statuses WHERE statusId = ?", (int(s),))
        row = cur.fetchone()
        if row:
            try:
                obj = json.loads(row[1]) if row[1] else {}
                if obj.get("orderTypeCode") == required_order_type:
                    return int(row[0])
            except Exception:
                pass
        return None

    # Otherwise match by code or name (name may be JSON)
    cur.execute("SELECT statusId, code, name, rawJson FROM ref_order_statuses")
    for statusId, code, name, rawJson in cur.fetchall():
        match = False

        # code direct
        if isinstance(code, str) and code.strip().lower() == s.lower():
            match = True
        else:
            # name maybe JSON or plain string
            name_val = _maybe_json(name)
            if isinstance(name_val, dict):
                for k in ("en", "EN", "default", "name"):
                    v = name_val.get(k)
                    if isinstance(v, str) and v.strip().lower() == s.lower():
                        match = True
                        break
            elif isinstance(name_val, str) and name_val.strip().lower() == s.lower():
                match = True

        if match:
            try:
                obj = json.loads(rawJson) if rawJson else {}
                if obj.get("orderTypeCode") == required_order_type:
                    return int(statusId)
            except Exception:
                pass

    return None

def validate_sales_orders(csv_path, db_path, account_name, *, log_callback: LogCallback = None):
    global _LOG_CALLBACK
    _LOG_CALLBACK = log_callback

    ensure_account_binding(db_path, account_name)

    if not os.path.exists(db_path):
        log(f"❌ Database not found: {db_path}")
        return 0, 0

    os.makedirs(_unmatched_dir(), exist_ok=True)

    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    ensure_tables(conn)

    # Read reference lookups once
    # (channels, price lists may have JSON in `name`; we handle that)
    # → these are created by your reference sync already. :contentReference[oaicite:8]{index=8}

    # Error buckets (write separate CSVs; preserve input headers)
    missing_customers = []
    unmatched_skus = []
    invalid_channels = []
    invalid_price_lists = []
    invalid_warehouses = []
    invalid_currencies = []
    invalid_shipping_methods = []
    invalid_statuses = []
    invalid_payment_methods = []
    invalid_payment_dates = []
    invalid_payment_amounts = []
    invalid_tax_dates = []
    invalid_delivery_dates = []
    invalid_placed_on = []
    processing_errors = []
    missing_required_row = []

    invalid_orders_summary = []
    skipped_rows_without_order_ref = 0


    with open_csv(csv_path) as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows_by_ref = {}
        for row in reader:
            # Keep the raw input row to preserve headers if needed
            order_ref = (_row_get(row, "order_ref") or "").strip()
            if not order_ref:
                # Skip totally malformed rows
                skipped_rows_without_order_ref += 1
                continue
            arr = rows_by_ref.setdefault(order_ref, [])
            arr.append(row)

    # Validate/group per order_ref
    orders_inserted = 0
    row_lines_inserted = 0

    for idx, (order_ref, rows) in enumerate(rows_by_ref.items(), start=1):
        # Pick the first row for order-level fields (CSV repeats them)
        r0 = rows[0]

        def _reject(reason: str, bucket: list):
            bucket.extend(rows)
            invalid_orders_summary.append((order_ref, reason, len(rows)))

        payment_amount_raw   = (_row_get(r0, "payment_amount") or "").strip()
        payment_date_raw     = (_row_get(r0, "payment_date") or "").strip()
        payment_ref          = (_row_get(r0, "payment_ref") or "").strip()
        payment_method_code  = (_row_get(r0, "payment_method_code") or "").strip()
        customer_email = (_row_get(r0, "customer_email") or "").strip().lower()
        placed_on_raw = (_row_get(r0, "placed_on") or "").strip()
        tax_date_raw = (_row_get(r0, "tax_date") or "").strip()
        delivery_date_raw = (_row_get(r0, "delivery_date") or "").strip()
        warehouse_field = (_row_get(r0, "warehouseId") or _row_get(r0, "warehouse") or "").strip()
        channel_field = (_row_get(r0, "channel") or "").strip()
        order_status_field = (_row_get(r0, "order_status") or "").strip()
        currency = (_row_get(r0, "currency") or "").strip()
        price_list_field = (_row_get(r0, "price_list") or "").strip()
        exchange_rate = _safe_float(_row_get(r0, "exchange_rate"), 1.0)
        shipping_method_field = (_row_get(r0, "shipping_method") or "").strip()

        # Delivery fields
        d_name = (_row_get(r0, "delivery_address_name") or "").strip()
        d1 = (_row_get(r0, "delivery_address_line1") or "").strip()
        d2 = (_row_get(r0, "delivery_address_line2") or "").strip()
        d3 = (_row_get(r0, "delivery_address_line3") or "").strip()
        d4 = (_row_get(r0, "delivery_address_line4") or "").strip()
        d_pc = (_row_get(r0, "delivery_postcode") or "").strip()
        d_country = (_row_get(r0, "delivery_country") or "").strip()
        d_tel = (_row_get(r0, "delivery_telephone") or "").strip()
        d_email = (_row_get(r0, "delivery_email") or "").strip().lower()

        try:
            # Lookups / validation
            cur.execute("""
                SELECT contactId FROM contact_catalogue
                WHERE LOWER(TRIM(primaryEmail)) = LOWER(TRIM(?))
                AND COALESCE(isCustomer, 0) = 1
                AND COALESCE(isSupplier, 0) = 0
                ORDER BY contactId
                LIMIT 1
            """, (customer_email,))
            c_row = cur.fetchone()
            contactId = int(c_row[0]) if c_row else None

            placed_on = _parse_date_or_datetime(placed_on_raw)
            tax_date = _parse_date_or_datetime(tax_date_raw) if tax_date_raw else None
            delivery_date = _parse_date_or_datetime(delivery_date_raw) if delivery_date_raw else None
            warehouseId = _warehouse_to_id(cur, warehouse_field)

            ch = _matches_name_or_code(cur, "ref_channels", "code", "name", channel_field)
            channelId = ch[0] if ch else None  # first selected column is rowid; but we only need channelId; fix:
            if ch:
                # Fetch real channelId from the same record again
                cur.execute("SELECT channelId FROM ref_channels WHERE rowid = ?", (ch[0],))
                row2 = cur.fetchone()
                channelId = int(row2[0]) if row2 else None

            pl = _matches_name_or_code(cur, "ref_price_lists", "code", "name", price_list_field)
            priceListId = None
            if pl:
                cur.execute("SELECT priceListId FROM ref_price_lists WHERE rowid = ?", (pl[0],))
                rr = cur.fetchone()
                if rr:
                    priceListId = int(rr[0])

            sm = _matches_name_or_code(cur, "ref_shipping_methods", "code", "name", shipping_method_field)
            shippingMethodId = None
            if sm:
                cur.execute("SELECT shippingMethodId FROM ref_shipping_methods WHERE rowid = ?", (sm[0],))
                rr = cur.fetchone()
                if rr:
                    shippingMethodId = int(rr[0])

            # Currency exists?
            cur.execute("SELECT 1 FROM ref_currencies WHERE UPPER(isoCode) = UPPER(?)", (currency,))
            currency_ok = cur.fetchone() is not None

            # Validate rows (SKUs can be blank for shipping/service lines)
            order_has_unmatched_skus = False
            prepared_rows = []
            order_has_invalid_rows = False
            line_no = 0
            for r in rows:
                line_no += 1
                sku = (_row_get(r, "item_sku") or "").strip()
                qty = _safe_float(_row_get(r, "item qty"))
                tax_code = (_row_get(r, "item_tax_code") or "").strip()  # e.g. T20, T0
                row_net = _safe_float(_row_get(r, "row_net"))
                row_tax = _safe_float(_row_get(r, "row_tax_amount"))
                item_name = (_row_get(r, "item_name") or "").strip()

                length_errors = product_field_length_errors(sku, item_name)
                if length_errors:
                    missing_required_row.append(row_with_validation_error(r, length_errors))
                    order_has_invalid_rows = True
                    continue

                productId = None
                row_type = "NOMINAL"
                if sku:
                    cur.execute("SELECT productId FROM product_catalogue WHERE SKU = ?", (sku,))
                    found = cur.fetchone()
                    if not found:
                        order_has_unmatched_skus = True
                        unmatched_skus.append(r)
                    else:
                        productId = int(found[0])
                        row_type = "PRODUCT"

                prepared_rows.append({
                    "order_ref": order_ref,
                    "line_number": line_no,
                    "item_name": item_name,
                    "item_sku": sku,
                    "item_qty": qty,
                    "item_tax_code": tax_code,
                    "row_net": row_net,
                    "row_tax_amount": row_tax,
                    "productId": productId,
                    "rowType": row_type,
                    "original_row_json": json.dumps(r, ensure_ascii=False)
                })

            # Are any payment fields present?
            has_any_payment = any([payment_amount_raw, payment_date_raw, payment_ref, payment_method_code])

            # Defaults (store NULLs if fully blank)
            payment_amount = None
            payment_date = None

            if has_any_payment:
                # amount is optional; if present, parse, else keep NULL
                payment_amount, payment_amount_error = _try_parse_float(payment_amount_raw)
                if payment_amount_error:
                    _reject(f"Invalid payment amount: {payment_amount_raw}", invalid_payment_amounts)
                    continue

                # date is optional; if present, must be valid
                if payment_date_raw:
                    payment_date = _parse_date_dmy(payment_date_raw)
                    if not payment_date:
                        _reject(f"Invalid payment date: {payment_date_raw}", invalid_payment_dates)
                        continue  # reject this order_ref

                # method code is optional; if present, must exist in refs
                if payment_method_code:
                    cur.execute("SELECT 1 FROM ref_payment_methods WHERE code = ? LIMIT 1", (payment_method_code,))
                    if cur.fetchone() is None:
                        _reject(f"Invalid payment method code: {payment_method_code}", invalid_payment_methods)
                        continue
            # else: all four are blank → leave
            # Drop invalid orders into relevant CSVs
            if not contactId:
                _reject(f"Customer email not found in contact_catalogue: {customer_email}", missing_customers)
                continue
            if not placed_on:
                _reject(f"Invalid placed_on date/datetime: {placed_on_raw}", invalid_placed_on)
                continue
            if tax_date_raw and not tax_date:
                _reject(f"Invalid tax_date date/datetime: {tax_date_raw}", invalid_tax_dates)
                continue
            if delivery_date_raw and not delivery_date:
                _reject(f"Invalid delivery_date date/datetime: {delivery_date_raw}", invalid_delivery_dates)
                continue
            if warehouseId is None:
                _reject(f"Warehouse not found: {warehouse_field}", invalid_warehouses)
                continue
            if channelId is None:
                _reject(f"Channel not found: {channel_field}", invalid_channels)
                continue
            if priceListId is None:
                _reject(f"Price list not found: {price_list_field}", invalid_price_lists)
                continue
            if not currency_ok:
                _reject(f"Currency not found in ref_currencies: {currency}", invalid_currencies)
                continue
            if order_has_unmatched_skus:
                # Ship/nominal lines are okay, but if any product row fails, we reject the whole order
                invalid_orders_summary.append((order_ref, "Contains one or more unmatched item_sku values", len(rows)))
                continue
            if order_has_invalid_rows:
                invalid_orders_summary.append((order_ref, "Contains an overlong SKU or product name", len(rows)))
                continue
            # shipping method can be optional; only flag if provided but invalid
            if shipping_method_field and shippingMethodId is None:
                _reject(f"Shipping method not found: {shipping_method_field}", invalid_shipping_methods)
                continue
            statusId = _status_to_id(cur, order_status_field, required_order_type="SO")
            if statusId is None:
                _reject(f"Order status not found/invalid for SO: {order_status_field}", invalid_statuses)
                continue

        except Exception as exc:
            _reject(f"Unexpected validation exception: {exc}", processing_errors)
            continue


        # Insert order header
        cur.execute(f"""
            INSERT INTO {ORDERS_TABLE} (
                order_ref, contactId, placed_on, tax_date, delivery_date, warehouseId, channelId,statusId, currency, priceListId, exchangeRate,
                shippingMethodId, payment_amount, payment_date, payment_ref, payment_method_code, delivery_name, delivery_line1, delivery_line2, delivery_line3, delivery_line4,
                delivery_postcode, delivery_country, delivery_countryIsoCode, delivery_telephone, delivery_email,
                source_csv_headers, source_csv_filename
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_ref, contactId, placed_on, tax_date, delivery_date, warehouseId, channelId, statusId, currency, priceListId, exchange_rate,
            shippingMethodId, payment_amount, payment_date, payment_ref, payment_method_code, d_name, d1, d2, d3, d4, d_pc, d_country, _country_to_iso(d_country), d_tel, d_email,
            json.dumps(list(rows[0].keys()), ensure_ascii=False), os.path.basename(csv_path)
        ))
        orders_inserted += 1

        # Insert rows
        for pr in prepared_rows:
            cur.execute(f"""
                INSERT INTO {ROWS_TABLE} (
                    order_ref, line_number, item_name, item_sku, item_qty, item_tax_code, row_net, row_tax_amount,
                    productId, rowType, original_row_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pr["order_ref"], pr["line_number"], pr["item_name"], pr["item_sku"], pr["item_qty"],
                pr["item_tax_code"], pr["row_net"], pr["row_tax_amount"], pr["productId"],
                pr["rowType"], pr["original_row_json"]
            ))
            row_lines_inserted += 1

    conn.commit()
    conn.close()

    total_orders = len(rows_by_ref)
    rejected_orders = total_orders - orders_inserted
    log(
        f"✅ Inserted {orders_inserted}/{total_orders} orders and {row_lines_inserted} row lines into SQLite "
        f"(rejected orders: {rejected_orders})."
    )
    if skipped_rows_without_order_ref:
        log(f"⚠️ Skipped {skipped_rows_without_order_ref} CSV rows with missing order_ref.")
    if rejected_orders:
        log("⚠️ Validation failures were detected. Review the generated unmatched CSV files for details.")
        preview = invalid_orders_summary[:10]
        for order, reason, row_count in preview:
            log(f"⚠️ Rejected order_ref={order} ({row_count} rows): {reason}")
        if len(invalid_orders_summary) > len(preview):
            log(f"⚠️ ... plus {len(invalid_orders_summary) - len(preview)} more rejected orders.")

    # Write invalid CSVs (exact original headers)
    def _write_csv(filename, rows, headers):
        if not rows:
            return
        out = os.path.join(_unmatched_dir(), f"{account_name}_{filename}")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            w.writerows(rows)
        log(f"⚠️ Wrote {len(rows)} rows → {out}")

    input_headers = list(rows_by_ref[next(iter(rows_by_ref))][0].keys()) if rows_by_ref else []
    _write_csv("so_missing_customers.csv",      missing_customers, input_headers)
    _write_csv("so_unmatched_skus.csv",         unmatched_skus,    input_headers)
    _write_csv("so_invalid_channels.csv",       invalid_channels,  input_headers)
    _write_csv("so_invalid_statuses.csv",       invalid_statuses, input_headers)
    _write_csv("so_invalid_price_lists.csv",    invalid_price_lists, input_headers)
    _write_csv("so_invalid_warehouses.csv",     invalid_warehouses, input_headers)
    _write_csv("so_invalid_currencies.csv",     invalid_currencies, input_headers)
    _write_csv("so_invalid_shipping_methods.csv", invalid_shipping_methods, input_headers)
    _write_csv("so_invalid_payment_methods.csv", invalid_payment_methods, input_headers)
    _write_csv("so_invalid_payment_dates.csv", invalid_payment_dates, input_headers)
    _write_csv("so_invalid_payment_amounts.csv", invalid_payment_amounts, input_headers)
    _write_csv("so_invalid_tax_dates.csv", invalid_tax_dates, input_headers)
    _write_csv("so_invalid_delivery_dates.csv", invalid_delivery_dates, input_headers)
    _write_csv("so_invalid_placed_on.csv", invalid_placed_on, input_headers)
    _write_csv("so_processing_errors.csv", processing_errors, input_headers)
    _write_csv("so_missing_required_row.csv", missing_required_row, [*input_headers, "validation_error"])

    return orders_inserted, row_lines_inserted


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python validator_sales_orders.py <account_name> <db_path>")
        sys.exit(1)
    account_name = sys.argv[1]
    db_path = sys.argv[2]
    csv_path = select_csv_file()
    if csv_path:
        validate_sales_orders(csv_path, db_path, account_name)
    else:
        log("No file selected.")
