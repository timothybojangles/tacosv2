# utils/validator_open_purchases.py

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

ORDERS_TABLE = "validated_purchase_orders"
ROWS_TABLE = "validated_purchase_order_rows"

MAX_ROWS_PER_ORDER = 240

TEMPLATE_HEADERS = [
    "order_ref",
    "price_list",
    "placed_on",
    "order_status",
    "delivery_date",
    "shipping_method",
    "currency",
    "exchangeRate",
    "payment_amount",
    "payment_date",
    "payment_ref",
    "payment_method_code",
    "supplier_email",
    "warehouseId",
    "channel",
    "sku",
    "quantity",
    "row_net",
    "row_tax",
    "tax_code",
    "nominal_code",
    "item_name",
]

LogCallback = Optional[Callable[[str], None]]

_LOG_CALLBACK: LogCallback = None


def log(msg: str):
    bp_log(f"[Open Purchases Validation] {msg}", _LOG_CALLBACK)


def select_csv_file():
    root = Tk(); root.withdraw()
    return filedialog.askopenfilename(
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Select CSV file for Open Purchases validation"
    )


def _unmatched_dir() -> str:
    return get_settings().unmatched_output_dir or UNMATCHED_DIR


def ensure_tables(conn: sqlite3.Connection):
    cur = conn.cursor()

    # Recreate validated tables each run
    cur.execute(f"DROP TABLE IF EXISTS {ORDERS_TABLE}")
    cur.execute(f"""
        CREATE TABLE {ORDERS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_ref TEXT UNIQUE NOT NULL,
            placed_on TEXT NOT NULL,                 -- YYYY-MM-DD
            delivery_date TEXT,                      -- YYYY-MM-DD (optional)
            currency TEXT NOT NULL,
            exchange_rate REAL,
            contactId INTEGER NOT NULL,
            channelId INTEGER,
            warehouseId INTEGER NOT NULL,
            statusId INTEGER NOT NULL,
            priceListId INTEGER,                     -- optional
            shippingMethodId INTEGER,                -- optional
            payment_amount REAL,
            payment_date TEXT,
            payment_ref TEXT,
            payment_method_code TEXT,

            -- bookkeeping
            orderId INTEGER,                         -- filled by sync
            source_csv_headers TEXT,
            source_csv_filename TEXT{processing_column_definitions()}
        )
    """)

    cur.execute(f"DROP TABLE IF EXISTS {ROWS_TABLE}")
    cur.execute(f"""
        CREATE TABLE {ROWS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_ref TEXT NOT NULL,
            line_number INTEGER NOT NULL,
            sku TEXT,
            quantity INTEGER NOT NULL,
            row_net REAL NOT NULL,
            row_tax REAL NOT NULL,
            tax_code TEXT NOT NULL,
            nominal_code TEXT,
            item_name TEXT,
            productId INTEGER,                       -- if resolved (sku)
            rowType TEXT,                            -- 'PRODUCT' or 'NOMINAL'
            original_row_json TEXT
        )
    """)

    # Helpful index
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{ROWS_TABLE}_ref ON {ROWS_TABLE}(order_ref)")
    conn.commit()


def _safe_float(v, default=None):
    try:
        s = (v or "").strip().replace(",", "")
        return float(s)
    except Exception:
        return default


def _round_4(value):
    if value is None:
        return None
    return round(value, 4)


def _parse_date_dmy(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return None


def _maybe_json(s):
    s = (s or "").strip()
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            return s
    return s


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


def _matches_name_or_code(cur, table, code_col, name_col, needle: str):
    """Match by exact code or name (name may be JSON) case-insensitively."""
    if not needle:
        return None
    n = needle.strip().lower()

    # code exact
    cur.execute(f"SELECT rowid, * FROM {table} WHERE LOWER({code_col}) = ?", (n,))
    row = cur.fetchone()
    if row:
        return row

    # name (may be JSON)
    cur.execute(f"SELECT rowid, {name_col} FROM {table}")
    for r in cur.fetchall():
        name_val = _maybe_json(r[1])
        if isinstance(name_val, dict):
            for k in ("en", "EN", "default", "name"):
                if k in name_val and isinstance(name_val[k], str) and name_val[k].strip().lower() == n:
                    return r
        elif isinstance(name_val, str):
            if name_val.strip().lower() == n:
                return r
    return None


def _status_to_id(cur, status_field: str, required_order_type: str = "PO"):
    s = (status_field or "").strip()
    if not s:
        return None

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

    cur.execute("SELECT statusId, code, name, rawJson FROM ref_order_statuses")
    for statusId, code, name, rawJson in cur.fetchall():
        match = False
        if isinstance(code, str) and code.strip().lower() == s.lower():
            match = True
        else:
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


def validate_open_purchases(csv_path, db_path, account_name, *, log_callback: LogCallback = None):
    global _LOG_CALLBACK
    _LOG_CALLBACK = log_callback

    ensure_account_binding(db_path, account_name)

    if not os.path.exists(db_path):
        log(f"Database not found: {db_path}")
        return 0, 0

    os.makedirs(_unmatched_dir(), exist_ok=True)

    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    ensure_tables(conn)

    # Error buckets (write exact input headers)
    missing_required_order = []
    missing_required_row = []
    invalid_channels = []
    invalid_price_lists = []
    invalid_warehouses = []
    invalid_statuses = []
    invalid_shipping_methods = []
    invalid_currencies = []
    unmatched_skus = []
    too_many_rows = []

    with open_csv(csv_path) as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        normalized_headers = [
            h.strip().lstrip("\ufeff") if isinstance(h, str) else h for h in headers
        ]
        if normalized_headers != headers:
            reader.fieldnames = normalized_headers
            headers = normalized_headers

        # Group rows by order_ref
        rows_by_ref = {}
        for idx, row in enumerate(reader, start=1):
            order_ref = (row.get("order_ref") or "").strip()
            if not order_ref:
                missing_required_order.append(row)
                log(f"Row {idx}: missing order_ref")
                continue
            rows_by_ref.setdefault(order_ref, []).append(row)

    orders_inserted = 0
    row_lines_inserted = 0

    for order_ref, rows in rows_by_ref.items():
        # Enforce 240 rows max
        if len(rows) > MAX_ROWS_PER_ORDER:
            too_many_rows.extend(rows)
            log(f"Order {order_ref}: too many rows ({len(rows)} > {MAX_ROWS_PER_ORDER})")
            continue

        r0 = rows[0]

        # Required order-level fields
        warehouse_field = (r0.get("warehouseId") or "").strip()
        placed_on_raw = (r0.get("placed_on") or "").strip()
        order_status_field = (r0.get("order_status") or "").strip()
        currency = (r0.get("currency") or "").strip()
        supplier_email = (r0.get("supplier_email") or "").strip().lower()
        channel_field = (r0.get("channel") or "").strip()

        # Optional order-level
        price_list_field = (r0.get("price_list") or "").strip()
        shipping_method_field = (r0.get("shipping_method") or "").strip()
        delivery_date_raw = (r0.get("delivery_date") or "").strip()
        exchange_rate = _round_4(_safe_float(r0.get("exchangeRate")))
        payment_amount = _round_4(_safe_float(r0.get("payment_amount")))
        payment_date_raw = (r0.get("payment_date") or "").strip()
        payment_ref = (r0.get("payment_ref") or "").strip()
        payment_method_code = (r0.get("payment_method_code") or "").strip()

        # Lookups
        # Contact: supplier only
        cur.execute("""
            SELECT contactId FROM contact_catalogue
            WHERE LOWER(TRIM(primaryEmail)) = LOWER(TRIM(?))
              AND COALESCE(isSupplier, 0) = 1
            ORDER BY contactId
            LIMIT 1
        """, (supplier_email,))
        c_row = cur.fetchone()
        if not c_row:
            # fallback: any supplier with this email
            cur.execute("""
                SELECT contactId FROM contact_catalogue
                WHERE LOWER(TRIM(primaryEmail)) = LOWER(TRIM(?)) AND COALESCE(isSupplier,0)=1
                ORDER BY contactId LIMIT 1
            """, (supplier_email,))
            c_row = cur.fetchone()
        contactId = int(c_row[0]) if c_row else None

        warehouseId = _warehouse_to_id(cur, warehouse_field)
        placed_on = _parse_date_dmy(placed_on_raw)
        delivery_date = _parse_date_dmy(delivery_date_raw) if delivery_date_raw else None
        payment_date = _parse_date_dmy(payment_date_raw) if payment_date_raw else None

        channelId = None
        if channel_field:
            ch = _matches_name_or_code(cur, "ref_channels", "code", "name", channel_field)
            if ch:
                cur.execute("SELECT channelId FROM ref_channels WHERE rowid = ?", (ch[0],))
                rr = cur.fetchone()
                if rr:
                    channelId = int(rr[0])

        pl = _matches_name_or_code(cur, "ref_price_lists", "code", "name", price_list_field) if price_list_field else None
        priceListId = None
        if pl:
            cur.execute("SELECT priceListId FROM ref_price_lists WHERE rowid = ?", (pl[0],))
            rr = cur.fetchone()
            if rr:
                priceListId = int(rr[0])

        sm = _matches_name_or_code(cur, "ref_shipping_methods", "code", "name", shipping_method_field) if shipping_method_field else None
        shippingMethodId = None
        if sm:
            cur.execute("SELECT shippingMethodId FROM ref_shipping_methods WHERE rowid = ?", (sm[0],))
            rr = cur.fetchone()
            if rr:
                shippingMethodId = int(rr[0])

        statusId = _status_to_id(cur, order_status_field, "PO")

        # Currency exists?
        cur.execute("SELECT 1 FROM ref_currencies WHERE UPPER(isoCode) = UPPER(?)", (currency,))
        currency_ok = cur.fetchone() is not None

        # Required order-level checks with explicit reasons
        fail_reasons = []
        if not warehouseId:
            fail_reasons.append(f"warehouseId '{warehouse_field}' not found")
            invalid_warehouses.extend(rows)
        if not placed_on:
            fail_reasons.append(f"placed_on '{placed_on_raw}' invalid (expected YYYY-MM-DD or dd/mm/yyyy)")
        if not statusId:
            fail_reasons.append(f"order_status '{order_status_field}' not found for PO")
            invalid_statuses.extend(rows)
        if not currency_ok:
            fail_reasons.append(f"currency '{currency}' not in ref_currencies")
            invalid_currencies.extend(rows)
        if not contactId:
            fail_reasons.append(f"supplier_email '{supplier_email}' not found as supplier (isSupplier=1)")
        if channel_field and not channelId:
            fail_reasons.append(f"channel '{channel_field}' not found in ref_channels")
            invalid_channels.extend(rows)

        if fail_reasons:
            log(f"Order {order_ref} failed required checks: " + "; ".join(fail_reasons))

        if not (warehouseId and placed_on and statusId and currency_ok and contactId):
            missing_required_order.extend(rows)
            continue

        # Validate rows
        prepared_rows = []
        has_row_errors = False
        line_no = 0
        for r in rows:
            line_no += 1
            sku = (r.get("sku") or "").strip()
            item_name = (r.get("item_name") or "").strip()
            qty = _round_4(_safe_float(r.get("quantity")))
            tax_code = (r.get("tax_code") or "").strip()
            row_net = _round_4(_safe_float(r.get("row_net")))
            row_tax = _round_4(_safe_float(r.get("row_tax")))
            nominal_code = (r.get("nominal_code") or "").strip()

            length_errors = product_field_length_errors(sku, item_name)

            # Required row-level: (sku or item_name), quantity, tax_code, row_net, row_tax
            if (not sku and not item_name) or qty is None or tax_code == "" or row_net is None or row_tax is None or length_errors:
                errors = list(length_errors)
                if not sku and not item_name:
                    errors.append("SKU or product name is required")
                missing_required_row.append(row_with_validation_error(r, errors))
                has_row_errors = True
                continue

            productId = None
            rowType = "NOMINAL"
            if sku:
                # Product lookup in your catalogue table
                cur.execute("SELECT productId FROM product_catalogue WHERE SKU = ?", (sku,))
                found = cur.fetchone()
                if not found:
                    unmatched_skus.append(r)
                    has_row_errors = True
                    continue
                productId = int(found[0])
                rowType = "PRODUCT"

            prepared_rows.append({
                "order_ref": order_ref,
                "line_number": line_no,
                "sku": sku or None,
                "quantity": qty,
                "row_net": row_net,
                "row_tax": row_tax,
                "tax_code": tax_code,
                "nominal_code": nominal_code or None,
                "item_name": item_name or None,
                "productId": productId,
                "rowType": rowType,
                "original_row_json": json.dumps(r, ensure_ascii=False)
            })

        if has_row_errors:
            continue

        # Insert order header (placeholders count matches column count)
        cur.execute(f"""
            INSERT INTO {ORDERS_TABLE} (
                order_ref, placed_on, delivery_date, currency, exchange_rate,
                contactId, channelId, warehouseId, statusId, priceListId, shippingMethodId,
                payment_amount, payment_date, payment_ref, payment_method_code,
                source_csv_headers, source_csv_filename
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_ref, placed_on, delivery_date, currency, exchange_rate,
            contactId, channelId, warehouseId, statusId, priceListId, shippingMethodId,
            payment_amount, payment_date or None, payment_ref or None, payment_method_code or None,
            json.dumps(headers, ensure_ascii=False), os.path.basename(csv_path)
        ))
        orders_inserted += 1

        for pr in prepared_rows:
            cur.execute(f"""
                INSERT INTO {ROWS_TABLE} (
                    order_ref, line_number, sku, quantity, row_net, row_tax, tax_code, nominal_code, item_name,
                    productId, rowType, original_row_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pr["order_ref"], pr["line_number"], pr["sku"], pr["quantity"], pr["row_net"], pr["row_tax"],
                pr["tax_code"], pr["nominal_code"], pr["item_name"], pr["productId"], pr["rowType"], pr["original_row_json"]
            ))
            row_lines_inserted += 1

    conn.commit()
    conn.close()

    log(f"Inserted {orders_inserted} orders and {row_lines_inserted} rows into SQLite.")

    # Write invalid CSVs with original headers
    def _write_csv(filename, rows, headers):
        if not rows:
            return
        out = os.path.join(_unmatched_dir(), f"{account_name}_{filename}")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        log(f"Wrote {len(rows)} rows -> {out}")

    _write_csv("purchase_missing_required_order.csv", missing_required_order, TEMPLATE_HEADERS)
    _write_csv("purchase_missing_required_row.csv", missing_required_row, [*TEMPLATE_HEADERS, "validation_error"])
    _write_csv("purchase_invalid_channels.csv", invalid_channels, TEMPLATE_HEADERS)
    _write_csv("purchase_invalid_price_lists.csv", invalid_price_lists, TEMPLATE_HEADERS)
    _write_csv("purchase_invalid_warehouses.csv", invalid_warehouses, TEMPLATE_HEADERS)
    _write_csv("purchase_invalid_statuses.csv", invalid_statuses, TEMPLATE_HEADERS)
    _write_csv("purchase_invalid_shipping_methods.csv", invalid_shipping_methods, TEMPLATE_HEADERS)
    _write_csv("purchase_invalid_currencies.csv", invalid_currencies, TEMPLATE_HEADERS)
    _write_csv("purchase_unmatched_skus.csv", unmatched_skus, TEMPLATE_HEADERS)
    _write_csv("purchase_too_many_rows.csv", too_many_rows, TEMPLATE_HEADERS)

    return orders_inserted, row_lines_inserted
