"""Custom field reference sync, CSV validation, and Brightpearl patch helpers."""
from __future__ import annotations

import csv
from csv_safety import open_csv
import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from .common import (
    processing_column_definitions,
    ensure_processing_columns,
    mark_rows_processed,
    unprocessed_where_clause,
    SYNC_DEBUG_LOG,
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    log,
    log_search_progress,
    log_payload_exchange,
    log_sync,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)
from .performance import estimate_record_count, record_api_update
from .settings import get_download_retry_settings, get_settings, get_upload_retry_settings
from .throttle import parse_int_header, sleep_with_log, throttle_decision

urllib3.disable_warnings(InsecureRequestWarning)

REF_CONTACT = "ref_pcf_contact"
REF_PRODUCT = "ref_pcf_product"
REF_ORDER = "ref_pcf_order"
VALIDATED_TABLE = "validated_pcf"

CUSTOM_FIELD_ENDPOINTS = {
    "customer": "/contact-service/customer/custom-field-meta-data/",
    "supplier": "/contact-service/supplier/custom-field-meta-data/",
    "product": "/product-service/product/custom-field-meta-data/",
    "sales": "/order-service/sale/custom-field-meta-data/",
    "purchase": "/order-service/purchase/custom-field-meta-data/",
}

ORDER_CATALOGUE_COLUMNS = [
    "orderId",
    "orderTypeId",
    "contactId",
    "orderStatusId",
    "orderStockStatusId",
    "createOn",
    "createdById",
    "customerRef",
    "orderPaymentStatusId",
    "updatedOn",
    "parentOrderId",
    "orderShippingStatusId",
    "warehouseId",
    "staffOwnerContactId",
    "taxDate",
    "departmentId",
    "deliveryDate",
]


def _normalize_type(value: str) -> str:
    raw = (value or "").strip().upper()
    raw = raw.replace(" ", "_")
    if raw == "TEXTAREA":
        return "TEXT_AREA"
    if raw == "YESNO":
        return "YES_NO"
    return raw


def _parse_date(value: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _normalize_yes_no(value: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    truthy = {"1", "YES", "Yes", "Y", "TRUE", "True", "true"}
    falsy = {"0", "NO", "No", "N", "FALSE", "False", "false"}
    if text in truthy:
        return "true"
    if text in falsy:
        return "false"
    return None


def _ensure_ref_table(conn: sqlite3.Connection, table: str) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE,
            name TEXT,
            customFieldType TEXT,
            required INTEGER,
            options TEXT,
            source TEXT
        )
        """
    )
    conn.commit()


def _ensure_validated_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(
        f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            contactId INTEGER,
            productId INTEGER,
            orderId INTEGER,
            pcf_id INTEGER NOT NULL,
            pcf_code TEXT NOT NULL,
            pcf_type TEXT NOT NULL,
            value_text TEXT,
            option_id INTEGER,
            source_csv_filename TEXT{processing_column_definitions()}
        )
        """
    )
    conn.commit()


def _ensure_order_catalogue_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS order_catalogue (
            orderId INTEGER PRIMARY KEY,
            orderTypeId INTEGER,
            contactId INTEGER,
            orderStatusId INTEGER,
            orderStockStatusId INTEGER,
            createOn TEXT,
            createdById INTEGER,
            customerRef TEXT,
            orderPaymentStatusId INTEGER,
            updatedOn TEXT,
            parentOrderId INTEGER,
            orderShippingStatusId INTEGER,
            warehouseId INTEGER,
            staffOwnerContactId INTEGER,
            taxDate TEXT,
            departmentId INTEGER,
            deliveryDate TEXT
        )
        """
    )
    conn.commit()


def _order_record_from_row(row: Iterable, order_type_id: int) -> Optional[Tuple]:
    if not isinstance(row, (list, tuple)):
        return None
    values = [None] * len(ORDER_CATALOGUE_COLUMNS)
    for index, _column in enumerate(ORDER_CATALOGUE_COLUMNS):
        if index < len(row):
            values[index] = row[index]
    if values[0] is None:
        return None
    try:
        values[0] = int(values[0])
    except (TypeError, ValueError):
        return None
    if values[1] is None:
        values[1] = order_type_id
    return tuple(values)


def _parse_response_list(raw_response: str) -> List[dict]:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError:
        return []
    data = payload.get("response", payload)
    if isinstance(data, dict):
        data = data.get("response", data.get("results", []))
    return data if isinstance(data, list) else []


def _download_with_retries(
    url: str,
    headers: dict,
    *,
    log_callback=None,
    cancel_token=None,
) -> Tuple[Optional[str], int, int]:
    max_retries, default_sleep_ms = get_download_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancel before GET request.", log_callback)
            return None, 0, 0

        json_response, throttle_ms, requests_remaining = send_request(
            url,
            headers,
            cancel_token=cancel_token,
            log_callback=log_callback,
        )
        if json_response:
            return json_response, throttle_ms, requests_remaining

        if attempt < max_retries:
            log(
                f"⚠️ GET attempt {attempt}/{max_retries} failed; retrying after {default_sleep_ms * attempt} ms.",
                log_callback,
            )
            sleep_with_log(
                default_sleep_ms * attempt,
                log_callback,
                cancel_token=cancel_token,
                reason="download retry backoff",
                log_file=SYNC_DEBUG_LOG,
            )

    return None, 0, 0


def _store_custom_fields(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[dict],
    *,
    source: Optional[str] = None,
) -> int:
    cur = conn.cursor()
    _ensure_ref_table(conn, table)
    if source:
        cur.execute(f"DELETE FROM {table} WHERE source = ?", (source,))
    else:
        cur.execute(f"DELETE FROM {table}")

    inserted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        pcf_id = row.get("id")
        code = row.get("code")
        if pcf_id is None or not code:
            continue
        options = row.get("options")
        options_json = json.dumps(options, separators=(",", ":")) if options is not None else None
        cur.execute(
            f"""
            INSERT OR REPLACE INTO {table} (
                id, code, name, customFieldType, required, options, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(pcf_id),
                str(code).strip(),
                row.get("name"),
                _normalize_type(str(row.get("customFieldType") or "")),
                1 if row.get("required") else 0,
                options_json,
                source,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def sync_custom_field_metadata(
    account_name: str,
    db_path: str,
    *,
    endpoint: str,
    table: str,
    source: Optional[str] = None,
    log_callback=None,
    cancel_token=None,
) -> int:
    ensure_account_binding(db_path, account_name)
    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    url = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}{endpoint}"
    json_response, throttle_ms, requests_remaining = _download_with_retries(
        url,
        credentials.headers,
        cancel_token=cancel_token,
        log_callback=log_callback,
    )
    if cancel_token and cancel_token.is_set():
        return 0
    if not json_response:
        log("⚠️ No response returned from Brightpearl.", log_callback)
        return 0

    rows = _parse_response_list(json_response)
    if not rows:
        log("⚠️ No custom fields found in response.", log_callback)
        return 0
    record_api_update(len(rows))

    conn = sqlite3.connect(db_path)
    inserted = _store_custom_fields(conn, table, rows, source=source)
    conn.close()

    if should_pause_for_throttle(requests_remaining, throttle_ms):
        log(
            f"⏳ Throttling after custom field sync; sleeping {throttle_ms} ms.",
            log_callback,
        )
        sleep_with_log(
            throttle_ms,
            log_callback,
            cancel_token=cancel_token,
            reason="throttle",
            log_file=SYNC_DEBUG_LOG,
        )

    return inserted


def sync_contact_custom_fields(
    account_name: str,
    db_path: str,
    *,
    contact_type: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    endpoint = CUSTOM_FIELD_ENDPOINTS.get(contact_type)
    if not endpoint:
        log(f"⚠️ Unknown contact custom field type: {contact_type}.", log_callback)
        return 0
    return sync_custom_field_metadata(
        account_name,
        db_path,
        endpoint=endpoint,
        table=REF_CONTACT,
        source=contact_type,
        log_callback=log_callback,
        cancel_token=cancel_token,
    )


def sync_product_custom_fields(
    account_name: str,
    db_path: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    return sync_custom_field_metadata(
        account_name,
        db_path,
        endpoint=CUSTOM_FIELD_ENDPOINTS["product"],
        table=REF_PRODUCT,
        source="product",
        log_callback=log_callback,
        cancel_token=cancel_token,
    )


def sync_order_custom_fields(
    account_name: str,
    db_path: str,
    *,
    order_type: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    endpoint = CUSTOM_FIELD_ENDPOINTS.get(order_type)
    if not endpoint:
        log(f"⚠️ Unknown order custom field type: {order_type}.", log_callback)
        return 0
    return sync_custom_field_metadata(
        account_name,
        db_path,
        endpoint=endpoint,
        table=REF_ORDER,
        source=order_type,
        log_callback=log_callback,
        cancel_token=cancel_token,
    )


def sync_order_catalogue(
    account_name: str,
    db_path: str,
    *,
    order_type_id: int,
    log_callback=None,
    cancel_token=None,
    append_mode: bool = False,
    append_first_result: Optional[int] = None,
) -> int:
    ensure_account_binding(db_path, account_name)
    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    api_url_catalogue = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "order-service/order-search"
    )

    first_result = int(append_first_result) if append_mode and append_first_result else 1
    more_pages_available = True
    all_orders = []
    order_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after syncing {order_counter} orders (so far).", log_callback)
            break

        paginated_url = (
            f"{api_url_catalogue}?orderTypeId={order_type_id}&pageSize=500&firstResult={first_result}"
        )
        json_response, next_throttle_period, requests_remaining = _download_with_retries(
            paginated_url,
            credentials.headers,
            cancel_token=cancel_token,
            log_callback=log_callback,
        )

        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after request (so far: {order_counter}).", log_callback)
            break

        if json_response:
            data = json.loads(json_response).get("response", {})
            results = data.get("results", [])
            all_orders.extend(results)
            order_counter += len(results)
            record_api_update(len(results))
            log(f"🧾 Synced {order_counter} orders", log_callback)

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

    if not all_orders:
        if cancel_token and cancel_token.is_set():
            return order_counter
        log("⚠️ No orders returned from Brightpearl.", log_callback)
        return order_counter

    if cancel_token and cancel_token.is_set():
        return order_counter

    conn = sqlite3.connect(db_path)
    _ensure_order_catalogue_table(conn)
    cur = conn.cursor()
    if not append_mode:
        cur.execute("DELETE FROM order_catalogue WHERE orderTypeId = ?", (order_type_id,))

    placeholders = ",".join(["?"] * len(ORDER_CATALOGUE_COLUMNS))
    column_list = ", ".join(ORDER_CATALOGUE_COLUMNS)
    for row in all_orders:
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancelled during DB write; partial commit.", log_callback)
            break
        record = _order_record_from_row(row, order_type_id)
        if record is None:
            continue
        cur.execute(
            f"""
            INSERT OR REPLACE INTO order_catalogue ({column_list})
            VALUES ({placeholders})
            """,
            record,
        )
    conn.commit()
    conn.close()
    return order_counter


def _load_pcf_reference(cur: sqlite3.Cursor, table: str) -> Dict[str, dict]:
    cur.execute(f"SELECT id, code, customFieldType, options FROM {table}")
    rows = {}
    for pcf_id, code, field_type, options_json in cur.fetchall():
        options = None
        if options_json:
            try:
                options = json.loads(options_json)
            except json.JSONDecodeError:
                options = None
        rows[str(code)] = {
            "id": int(pcf_id),
            "code": str(code),
            "type": _normalize_type(str(field_type or "")),
            "options": options or {},
        }
    return rows


def _resolve_select_option(value: str, options: dict) -> Tuple[Optional[int], Optional[str]]:
    text = str(value).strip()
    if not text:
        return None, "Empty SELECT value"
    if text in options:
        return int(text), None
    matches = []
    lower_text = text.lower()
    for option in options.values():
        option_value = option.get("value")
        if option_value is None:
            continue
        if str(option_value) == text or str(option_value).lower() == lower_text:
            matches.append(int(option.get("id")))
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "Ambiguous SELECT value"
    return None, "Unknown SELECT value"


def _lookup_contact_id(cur: sqlite3.Cursor, email: str) -> Optional[int]:
    cur.execute(
        """
        SELECT contactId
        FROM contact_catalogue
        WHERE LOWER(TRIM(primaryEmail)) = LOWER(TRIM(?))
        ORDER BY contactId
        LIMIT 1
        """,
        (email,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _lookup_product_id(cur: sqlite3.Cursor, sku: str) -> Optional[int]:
    cur.execute(
        """
        SELECT productId
        FROM product_catalogue
        WHERE LOWER(TRIM(SKU)) = LOWER(TRIM(?))
        ORDER BY productId
        LIMIT 1
        """,
        (sku,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _lookup_order_id(cur: sqlite3.Cursor, order_ref: str) -> Optional[int]:
    cur.execute(
        """
        SELECT orderId
        FROM order_catalogue
        WHERE LOWER(TRIM(customerRef)) = LOWER(TRIM(?))
        ORDER BY orderId
        LIMIT 1
        """,
        (order_ref,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _entity_type_for_header(header: str) -> Optional[str]:
    header = (header or "").strip()
    if header == "contactEmail":
        return "contact"
    if header == "sku":
        return "product"
    if header == "orderRef":
        return "order"
    return None


def _validate_custom_field_value(raw_value: object, entry: dict) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    field_type = entry["type"]
    value_text: Optional[str] = None
    option_id: Optional[int] = None
    error: Optional[str] = None

    if field_type == "SELECT":
        option_id, error = _resolve_select_option(raw_value, entry["options"])
        if option_id is not None:
            value_text = str(option_id)
    elif field_type == "YES_NO":
        value_text = _normalize_yes_no(raw_value)
        if value_text is None:
            error = "Invalid YES_NO value"
    elif field_type == "INTEGER":
        try:
            value_text = str(int(str(raw_value).replace(",", "").strip()))
        except ValueError:
            error = "Invalid INTEGER value"
    elif field_type == "DATE":
        value_text = _parse_date(raw_value)
        if value_text is None:
            error = "Invalid DATE value"
    else:
        value_text = str(raw_value)

    return value_text, option_id, error


def validate_product_custom_fields_from_import_csv(
    csv_path: str,
    db_path: str,
    account_name: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    ensure_account_binding(db_path, account_name)
    if not os.path.exists(db_path):
        log(f"❌ Database not found: {db_path}", log_callback)
        return 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    _ensure_validated_table(conn)

    inserted = 0
    unmatched_rows: List[dict] = []
    unmatched_codes: List[dict] = []
    fieldnames: List[str] = []

    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        if not reader.fieldnames:
            log("⚠️ CSV has no headers.", log_callback)
            conn.close()
            return 0

        fieldnames = list(reader.fieldnames)
        sku_header = "sku"
        if sku_header not in fieldnames:
            log("⚠️ Product CSV has no sku column; skipping custom field validation.", log_callback)
            conn.close()
            return 0

        try:
            ref_map = _load_pcf_reference(cur, REF_PRODUCT)
        except sqlite3.OperationalError:
            log("⚠️ Product custom field reference data not found. Run product PCF sync first.", log_callback)
            conn.close()
            return 0

        pcf_codes = [header for header in fieldnames if str(header).strip().startswith("PCF_")]
        valid_codes: List[str] = []
        for code in pcf_codes:
            if code in ref_map:
                valid_codes.append(code)
            else:
                unmatched_codes.append({"pcf_code": code, "reason": "Unknown PCF code"})

        if not pcf_codes:
            conn.commit()
            conn.close()
            return 0

        for row in reader:
            if cancel_token and cancel_token.is_set():
                break
            sku = (row.get(sku_header) or "").strip()
            if not sku:
                row_copy = dict(row)
                row_copy["pcf_code"] = ""
                row_copy["reason"] = "Missing sku"
                unmatched_rows.append(row_copy)
                continue

            for code in valid_codes:
                raw_value = row.get(code)
                if raw_value is None or str(raw_value).strip() == "":
                    continue

                entry = ref_map[code]
                value_text, option_id, error = _validate_custom_field_value(raw_value, entry)
                if error:
                    row_copy = dict(row)
                    row_copy["pcf_code"] = code
                    row_copy["reason"] = error
                    unmatched_rows.append(row_copy)
                    continue

                cur.execute(
                    f"""
                    INSERT INTO {VALIDATED_TABLE} (
                        entity_type,
                        entity_key,
                        contactId,
                        productId,
                        orderId,
                        pcf_id,
                        pcf_code,
                        pcf_type,
                        value_text,
                        option_id,
                        source_csv_filename
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "product",
                        sku,
                        None,
                        None,
                        None,
                        entry["id"],
                        entry["code"],
                        entry["type"],
                        value_text,
                        option_id,
                        os.path.basename(csv_path),
                    ),
                )
                inserted += 1

    conn.commit()
    conn.close()

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)

    if unmatched_codes and unmatched_dir:
        codes_file = os.path.join(
            unmatched_dir,
            f"{account_name}_product_import_unmatched_pcf_codes.csv",
        )
        with open(codes_file, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["pcf_code", "reason"])
            writer.writeheader()
            writer.writerows(unmatched_codes)
        log(
            f"⚠️ {len(unmatched_codes)} unmatched product-import PCF codes written to {codes_file}",
            log_callback,
        )

    if unmatched_rows and unmatched_dir:
        rows_file = os.path.join(
            unmatched_dir,
            f"{account_name}_product_import_unmatched_pcf_rows.csv",
        )
        row_fieldnames = fieldnames + ["pcf_code", "reason"]
        with open(rows_file, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=row_fieldnames)
            writer.writeheader()
            writer.writerows(unmatched_rows)
        log(
            f"⚠️ {len(unmatched_rows)} unmatched product-import PCF rows written to {rows_file}",
            log_callback,
        )

    log(
        f"✅ Product import custom fields validated. Inserted {inserted} rows into {VALIDATED_TABLE}.",
        log_callback,
    )
    return inserted


def validate_custom_fields_csv(
    csv_path: str,
    db_path: str,
    account_name: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    ensure_account_binding(db_path, account_name)
    if not os.path.exists(db_path):
        log(f"❌ Database not found: {db_path}", log_callback)
        return 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    _ensure_validated_table(conn)

    inserted = 0
    unmatched_rows: List[dict] = []
    unmatched_codes: List[dict] = []

    fieldnames: List[str] = []

    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        if not reader.fieldnames:
            log("⚠️ CSV has no headers.", log_callback)
            conn.close()
            return 0

        fieldnames = list(reader.fieldnames)
        entity_header = fieldnames[0]
        entity_type = _entity_type_for_header(entity_header)
        if entity_type is None:
            log("❌ CSV must start with contactEmail, sku, or orderRef.", log_callback)
            conn.close()
            return 0

        ref_table = {
            "contact": REF_CONTACT,
            "product": REF_PRODUCT,
            "order": REF_ORDER,
        }[entity_type]
        try:
            ref_map = _load_pcf_reference(cur, ref_table)
        except sqlite3.OperationalError:
            log("⚠️ Custom field reference data not found. Run the sync first.", log_callback)
            conn.close()
            return 0

        pcf_codes = fieldnames[1:]
        valid_codes = []
        for code in pcf_codes:
            if code in ref_map:
                valid_codes.append(code)
            else:
                unmatched_codes.append({"pcf_code": code, "reason": "Unknown PCF code"})

        for row in reader:
            if cancel_token and cancel_token.is_set():
                break
            entity_value = (row.get(entity_header) or "").strip()
            if not entity_value:
                row_copy = dict(row)
                row_copy["pcf_code"] = ""
                row_copy["reason"] = f"Missing {entity_header}"
                unmatched_rows.append(row_copy)
                continue

            if entity_type == "contact":
                entity_id = _lookup_contact_id(cur, entity_value)
            elif entity_type == "product":
                entity_id = _lookup_product_id(cur, entity_value)
            else:
                entity_id = _lookup_order_id(cur, entity_value)

            if entity_id is None:
                row_copy = dict(row)
                row_copy["pcf_code"] = ""
                row_copy["reason"] = f"Unknown {entity_header}"
                unmatched_rows.append(row_copy)
                continue

            for code in valid_codes:
                raw_value = row.get(code)
                if raw_value is None or str(raw_value).strip() == "":
                    continue
                entry = ref_map[code]
                field_type = entry["type"]
                value_text, option_id, error = _validate_custom_field_value(raw_value, entry)

                if error:
                    row_copy = dict(row)
                    row_copy["pcf_code"] = code
                    row_copy["reason"] = error
                    unmatched_rows.append(row_copy)
                    continue

                cur.execute(
                    f"""
                    INSERT INTO {VALIDATED_TABLE} (
                        entity_type,
                        entity_key,
                        contactId,
                        productId,
                        orderId,
                        pcf_id,
                        pcf_code,
                        pcf_type,
                        value_text,
                        option_id,
                        source_csv_filename
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_type,
                        entity_value,
                        entity_id if entity_type == "contact" else None,
                        entity_id if entity_type == "product" else None,
                        entity_id if entity_type == "order" else None,
                        entry["id"],
                        entry["code"],
                        field_type,
                        value_text,
                        option_id,
                        os.path.basename(csv_path),
                    ),
                )
                inserted += 1

    conn.commit()
    conn.close()

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)

    if unmatched_codes:
        codes_file = os.path.join(unmatched_dir, f"{account_name}_unmatched_pcf_codes.csv")
        with open(codes_file, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["pcf_code", "reason"])
            writer.writeheader()
            writer.writerows(unmatched_codes)
        log(f"⚠️ {len(unmatched_codes)} unmatched PCF codes written to {codes_file}", log_callback)

    if unmatched_rows:
        rows_file = os.path.join(unmatched_dir, f"{account_name}_unmatched_pcf_rows.csv")
        row_fieldnames = fieldnames + ["pcf_code", "reason"]
        with open(rows_file, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=row_fieldnames)
            writer.writeheader()
            writer.writerows(unmatched_rows)
        log(f"⚠️ {len(unmatched_rows)} unmatched rows written to {rows_file}", log_callback)

    log(f"✅ Custom fields validated. Inserted {inserted} rows into {VALIDATED_TABLE}.", log_callback)
    return inserted


def _build_patch_payload(records: List[Tuple[str, str, Optional[str], Optional[int]]]) -> List[dict]:
    payload = []
    for pcf_code, pcf_type, value_text, option_id in records:
        if pcf_type == "SELECT":
            if option_id is None:
                continue
            value = {"id": option_id}
        elif pcf_type == "INTEGER":
            try:
                value = int(value_text) if value_text is not None else None
            except ValueError:
                continue
        elif pcf_type == "YES_NO":
            if value_text is None:
                continue
            value = value_text == "true"
        else:
            value = value_text

        payload.append({"op": "add", "path": f"/{pcf_code}", "value": value})
    return payload


def _patch_custom_fields(
    url: str,
    headers: dict,
    payload: List[dict],
    *,
    log_callback=None,
    cancel_token=None,
) -> bool:
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before PATCH.", log_callback)
            return False
        try:
            resp = requests.patch(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("PATCH", url, payload, resp, log_callback)
            log_sync(f"📮 PATCH {url} → {resp.status_code}", log_callback)

            if resp.status_code in {200, 204}:
                record_api_update(estimate_record_count(payload))
                requests_remaining = parse_int_header(resp.headers, "brightpearl-requests-remaining", 2)
                throttle_ms = parse_int_header(resp.headers, "brightpearl-next-throttle-period", 0)
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
                return True
            log_sync(f"❌ PATCH failed (attempt {attempt}) {resp.status_code}", log_callback)
            try:
                log_sync(f"📥 Response body:\n{resp.text}", log_callback)
            except Exception as exc:
                log_sync(f"⚠️ Could not read response body: {exc}", log_callback)
        except requests.RequestException as exc:
            log_sync(f"❌ PATCH exception (attempt {attempt}): {exc}", log_callback)

        sleep_with_log(
            default_sleep_ms * attempt,
            log_callback,
            cancel_token=cancel_token,
            reason="retry backoff",
            log_file=SYNC_DEBUG_LOG,
        )

    return False


def sync_custom_fields_to_brightpearl(
    account_name: str,
    db_path: str,
    *,
    log_callback=None,
    progress_callback=None,
    cancel_token=None,
) -> int:
    ensure_account_binding(db_path, account_name)
    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log_sync(str(exc), log_callback)
        return 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        ensure_processing_columns(conn, VALIDATED_TABLE)
        cur.execute(
            f"""
            SELECT id, entity_type, entity_key, contactId, productId, orderId, pcf_code, pcf_type, value_text, option_id
            FROM {VALIDATED_TABLE}
            WHERE {unprocessed_where_clause()}
            ORDER BY entity_type, COALESCE(contactId, productId, orderId), id
            """
        )
    except sqlite3.OperationalError:
        conn.close()
        log_sync("⚠️ No validated custom fields found. Upload and validate a CSV first.", log_callback)
        return 0

    rows = cur.fetchall()
    if not rows:
        log_sync("⚠️ No validated custom fields to sync.", log_callback)
        conn.close()
        return 0

    lookup_conn = sqlite3.connect(db_path)
    lookup_cur = lookup_conn.cursor()
    grouped: Dict[Tuple[str, int], List[Tuple[int, str, str, Optional[str], Optional[int]]]] = {}
    for (
        row_id,
        entity_type,
        entity_key,
        contact_id,
        product_id,
        order_id,
        code,
        field_type,
        value_text,
        option_id,
    ) in rows:
        entity_id = contact_id or product_id or order_id
        if entity_id is None and entity_type == "product" and entity_key:
            entity_id = _lookup_product_id(lookup_cur, str(entity_key))
        if entity_id is None:
            continue
        key = (entity_type, int(entity_id))
        grouped.setdefault(key, []).append((row_id, code, field_type, value_text, option_id))
    lookup_conn.close()

    total = len(grouped)
    synced = 0

    headers = {
        **credentials.headers,
        "Content-Type": "application/json",
    }

    for index, ((entity_type, entity_id), records) in enumerate(grouped.items(), start=1):
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Custom field sync cancelled.", log_callback)
            break

        payload = _build_patch_payload([(code, field_type, value_text, option_id) for _row_id, code, field_type, value_text, option_id in records])
        if not payload:
            log_sync(f"⚠️ Skipping {entity_type} {entity_id}: no payload to send.", log_callback)
            continue

        if entity_type == "contact":
            path = f"/contact-service/contact/{entity_id}/custom-field"
        elif entity_type == "product":
            path = f"/product-service/product/{entity_id}/custom-field"
        else:
            path = f"/order-service/order/{entity_id}/custom-field"

        url = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}{path}"
        if _patch_custom_fields(url, headers, payload, log_callback=log_callback, cancel_token=cancel_token):
            mark_rows_processed(conn, VALIDATED_TABLE, [record[0] for record in records], f"Patched {entity_type} {entity_id} custom fields")
            conn.commit()
            synced += 1

        if progress_callback:
            progress_callback(index, total)

    if not progress_callback:
        log_sync("PROGRESS:100", log_callback)

    conn.close()
    return synced
