"""Product import helpers for syncing reference data and creating products."""
from __future__ import annotations

import csv
from csv_safety import open_csv, open_table
import json
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from .common import (
    processing_column_definitions,
    ensure_processing_columns,
    mark_table_key_processed,
    mark_rows_processed,
    unprocessed_where_clause,
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    log,
    log_search_progress,
    log_payload,
    log_payload_exchange,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)
from .performance import estimate_record_count, record_api_update
from reference_data import fetch_and_store_reference_tables
from .settings import get_settings, get_upload_retry_settings
from row_validation import (
    PRODUCT_NAME_MAX_LENGTH,
    SKU_MAX_LENGTH,
    product_field_length_errors,
    row_with_validation_error,
)

TEMPLATE_HEADERS: Sequence[str] = (
    "productName",
    "brand",
    "Type",
    "category",
    "sku",
    "ean",
    "upc",
    "isbn",
    "mpn",
    "barcode",
    "stockTracked",
    "weight",
    "dimensions.width",
    "dimensions.length",
    "dimensions.height",
    "taxable",
    "taxCode",
    "productCondition",
    "description.text",
    "shortDescription.text",
    "ReplaceHeaderWith_optionName_FillCellsWith_Value",
    "seasons",
    "primarySupplier",
    "Supplier",
    "nominalCodeStock",
    "nominalCodePurchases",
    "nominalCodeSales",
    "bundleTrueFalse",
    "bundle_composition",
    "reportingCategory",
    "reportingSubcategory",
    "reportingSeason",
)

OPTION_PLACEHOLDER = "ReplaceHeaderWith_optionName_FillCellsWith_Value"
BASE_HEADERS = set(TEMPLATE_HEADERS)

IDENTITY_MAX_LENGTH = 50
LOG_FULL_PAYLOADS = os.getenv("BP_PRODUCT_IMPORT_LOG_PAYLOADS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

PRODUCT_UPDATE_FIELDS: Sequence[str] = (
    "brandId",
    "collectionId",
    "productGroupId",
    "productTypeId",
    "identity.sku",
    "identity.ean",
    "identity.upc",
    "identity.isbn",
    "identity.mpn",
    "identity.barcode",
    "stock.stockTracked",
    "stock.weight",
    "dimensions.width",
    "dimensions.length",
    "dimensions.height",
    "financialDetails.taxable",
    "financialDetails.taxCode",
    "salesChannels.productName",
    "salesChannels.productCondition",
    "salesChannels.categories",
    "salesChannels.description.text",
    "salesChannels.shortDescription.text",
    "nominalCodeStock",
    "nominalCodePurchases",
    "nominalCodeSales",
    "reporting.categoryId",
    "reporting.subcategoryId",
    "reporting.seasonId",
    "seasonIds",
    "variations",
    "composition.bundle",
    "composition.bundleComponents",
    "primarySupplier",
    "Supplier",
)

def _flatten_contact_rows(rows: Iterable[Iterable]) -> Dict[int, dict]:
    element_titles = [
        "contactId",
        "primaryEmail",
        "secondaryEmail",
        "tertiaryEmail",
        "firstName",
        "lastName",
        "isSupplier",
        "companyName",
        "isStaff",
        "isCustomer",
        "createdOn",
        "updatedOn",
        "lastContactedOn",
        "lastOrderedOn",
        "nominalCode",
        "isPrimary",
        "pri",
        "sec",
        "mob",
        "exactCompanyName",
        "title",
    ]
    catalogue: Dict[int, dict] = {}
    for row in rows:
        item = {element_titles[i]: row[i] for i in range(len(row))}
        contact_id = item.get("contactId")
        if contact_id is not None:
            catalogue[int(contact_id)] = item
    return catalogue


def _update_supplier_contact_catalogue(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Fetch supplier-only contacts for the product import reference sync."""
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    api_url_catalogue = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "contact-service/contact-search"
    )

    first_result = 1
    more_pages_available = True
    all_contacts = []
    contact_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after syncing {contact_counter} contacts (so far).", log_callback)
            break

        paginated_url = (
            f"{api_url_catalogue}?isSupplier=true&pageSize=500&firstResult={first_result}"
        )
        json_response, next_throttle_period, requests_remaining = send_request(
            paginated_url, credentials.headers, cancel_token=cancel_token, log_callback=log_callback
        )

        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after request (so far: {contact_counter}).", log_callback)
            break

        if json_response:
            data = json.loads(json_response).get("response", {})
            results = data.get("results", [])
            all_contacts.extend(results)
            contact_counter += len(results)
            record_api_update(len(results))
            log(f"📦 Synced {contact_counter} supplier contacts", log_callback)

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

    if not all_contacts:
        if cancel_token and cancel_token.is_set():
            return contact_counter
        log("⚠️ No supplier contacts returned from Brightpearl.", log_callback)
        return contact_counter

    if cancel_token and cancel_token.is_set():
        return contact_counter

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_catalogue (
            contactId INTEGER PRIMARY KEY,
            primaryEmail TEXT,
            secondaryEmail TEXT,
            tertiaryEmail TEXT,
            firstName TEXT,
            lastName TEXT,
            isSupplier BOOLEAN,
            companyName TEXT,
            isStaff BOOLEAN,
            isCustomer BOOLEAN,
            createdOn TEXT,
            updatedOn TEXT,
            lastContactedOn TEXT,
            lastOrderedOn TEXT,
            nominalCode INTEGER,
            isPrimary BOOLEAN,
            pri TEXT,
            sec TEXT,
            mob TEXT,
            exactCompanyName TEXT,
            title TEXT
        )
        """
    )
    cursor.execute("DELETE FROM contact_catalogue")

    contact_catalogue = _flatten_contact_rows(all_contacts)
    for contact in contact_catalogue.values():
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancelled during DB write; partial commit.", log_callback)
            break
        cursor.execute(
            """
            INSERT INTO contact_catalogue (
                contactId,
                primaryEmail,
                secondaryEmail,
                tertiaryEmail,
                firstName,
                lastName,
                isSupplier,
                companyName,
                isStaff,
                isCustomer,
                createdOn,
                updatedOn,
                lastContactedOn,
                lastOrderedOn,
                nominalCode,
                isPrimary,
                pri,
                sec,
                mob,
                exactCompanyName,
                title
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contact.get("contactId"),
                contact.get("primaryEmail"),
                contact.get("secondaryEmail"),
                contact.get("tertiaryEmail"),
                contact.get("firstName"),
                contact.get("lastName"),
                contact.get("isSupplier"),
                contact.get("companyName"),
                contact.get("isStaff"),
                contact.get("isCustomer"),
                contact.get("createdOn"),
                contact.get("updatedOn"),
                contact.get("lastContactedOn"),
                contact.get("lastOrderedOn"),
                contact.get("nominalCode"),
                contact.get("isPrimary"),
                contact.get("pri"),
                contact.get("sec"),
                contact.get("mob"),
                contact.get("exactCompanyName"),
                contact.get("title"),
            ),
        )
    conn.commit()
    conn.close()
    return contact_counter


def _safe_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row_value(row: object, key: str) -> Optional[object]:
    if isinstance(row, sqlite3.Row):
        if key in row.keys():
            return row[key]
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]  # type: ignore[index]
    except Exception:
        return None


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _safe_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "y"}:
        return True
    if text in {"false", "no", "0", "n"}:
        return False
    return None


def _split_list(value: Optional[str]) -> List[str]:
    if value is None:
        return []
    raw = str(value)
    items: List[str] = []
    for chunk in raw.replace("|", ",").split(","):
        cleaned = chunk.strip()
        if cleaned:
            items.append(cleaned)
    return items


def _split_pipe_list(value: Optional[str]) -> List[str]:
    if value is None:
        return []
    raw = str(value)
    items: List[str] = []
    for chunk in raw.split("|"):
        cleaned = chunk.strip()
        if cleaned:
            items.append(cleaned)
    return items


def _parse_variations(raw_value: Optional[str]) -> List[Tuple[int, int]]:
    if not raw_value:
        return []
    results: List[Tuple[int, int]] = []
    for chunk in str(raw_value).replace(",", "|").split("|"):
        if not chunk.strip() or ":" not in chunk:
            continue
        option_text, value_text = chunk.split(":", 1)
        option_id = _safe_int(option_text)
        value_id = _safe_int(value_text)
        if option_id is not None and value_id is not None:
            results.append((option_id, value_id))
    return results


def _parse_components_by_id(raw_components: Optional[str]) -> List[Tuple[int, float]]:
    if not raw_components:
        return []
    components: List[Tuple[int, float]] = []
    for chunk in str(raw_components).replace(",", "|").split("|"):
        if not chunk.strip() or ":" not in chunk:
            continue
        product_id_text, qty = chunk.split(":", 1)
        product_id = _safe_int(product_id_text)
        qty_value = _safe_float(qty)
        if product_id is not None and qty_value is not None:
            components.append((product_id, qty_value))
    return components


def _ensure_reference_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_brands (
            brandId INTEGER PRIMARY KEY,
            brandName TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_product_category (
            id INTEGER PRIMARY KEY,
            name TEXT,
            parentId INTEGER,
            active BOOLEAN,
            createdOn TEXT,
            createdById INTEGER,
            updatedOn TEXT,
            updatedBy INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_option (
            id INTEGER PRIMARY KEY,
            name TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_option_values (
            optionId INTEGER,
            optionName TEXT,
            optionValueId INTEGER,
            optionValueName TEXT,
            PRIMARY KEY (optionId, optionValueId)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_type (
            id INTEGER PRIMARY KEY,
            name TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_season (
            id INTEGER PRIMARY KEY,
            name TEXT,
            description TEXT,
            dateFrom TEXT,
            dateTo TEXT
        )
        """
    )
    conn.commit()


def _ensure_import_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS product_import_core")
    cur.execute("DROP TABLE IF EXISTS product_import_options")
    cur.execute("DROP TABLE IF EXISTS product_import_option_values")
    cur.execute("DROP TABLE IF EXISTS product_import_category")
    cur.execute("DROP TABLE IF EXISTS product_import_seasons")

    cur.execute(
        f"""
        CREATE TABLE product_import_core (
            sku TEXT PRIMARY KEY,
            productName TEXT,
            brand TEXT,
            brandId INTEGER,
            productType TEXT,
            productTypeId INTEGER,
            ean TEXT,
            upc TEXT,
            isbn TEXT,
            mpn TEXT,
            barcode TEXT,
            stockTracked BOOLEAN,
            weight REAL,
            dimensions_width REAL,
            dimensions_length REAL,
            dimensions_height REAL,
            taxable BOOLEAN,
            taxCode TEXT,
            productCondition TEXT,
            description_text TEXT,
            shortDescription_text TEXT,
            primarySupplier TEXT,
            primarySupplierId INTEGER,
            suppliers TEXT,
            supplierContactIds TEXT,
            nominalCodeStock TEXT,
            nominalCodePurchases TEXT,
            nominalCodeSales TEXT,
            bundleTrueFalse TEXT,
            bundleComponents TEXT,
            reportingCategory TEXT,
            reportingCategoryId INTEGER,
            reportingSubcategory TEXT,
            reportingSubcategoryId INTEGER,
            reportingSeason TEXT,
            reportingSeasonId INTEGER{processing_column_definitions()}
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE product_import_options (
            sku TEXT,
            option_name TEXT,
            option_id INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE product_import_option_values (
            sku TEXT,
            option_name TEXT,
            option_id INTEGER,
            option_value_name TEXT,
            option_value_id INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE product_import_category (
            sku TEXT,
            category_name TEXT,
            category_id INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE product_import_seasons (
            sku TEXT,
            season_name TEXT,
            season_id INTEGER
        )
        """
    )
    conn.commit()


def _ensure_product_update_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_catalogue_core (
            productId INTEGER PRIMARY KEY,
            sku TEXT,
            brandId INTEGER,
            productTypeId INTEGER,
            collectionId INTEGER,
            productGroupId INTEGER,
            salesChannelName TEXT,
            productName TEXT,
            productCondition TEXT,
            description_text TEXT,
            shortDescription_text TEXT,
            nominalCodeStock TEXT,
            nominalCodePurchases TEXT,
            nominalCodeSales TEXT,
            reportingCategoryId INTEGER,
            reportingSubcategoryId INTEGER,
            reportingSeasonId INTEGER,
            bundleFlag INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_catalogue_category (
            productId INTEGER,
            categoryCode TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_catalogue_options (
            productId INTEGER,
            optionId INTEGER,
            optionValueId INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_catalogue_seasons (
            productId INTEGER,
            seasonId INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_catalogue_bundle (
            productId INTEGER,
            componentProductId INTEGER,
            componentQty REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_update_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_update_options (
            productId INTEGER,
            option_name TEXT,
            option_id INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS product_update_option_values (
            productId INTEGER,
            option_name TEXT,
            option_id INTEGER,
            option_value_name TEXT,
            option_value_id INTEGER
        )
        """
    )
    conn.commit()


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f"\"{escaped}\""


def _store_update_config(conn: sqlite3.Connection, identifier: str, fields: Sequence[str]) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM product_update_config")
    cur.execute(
        "INSERT INTO product_update_config (key, value) VALUES (?, ?)",
        ("identifier", identifier),
    )
    cur.execute(
        "INSERT INTO product_update_config (key, value) VALUES (?, ?)",
        ("fields", json.dumps(list(fields))),
    )
    conn.commit()


def _load_update_config(conn: sqlite3.Connection) -> Tuple[Optional[str], List[str]]:
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM product_update_config")
    rows = {key: value for key, value in cur.fetchall()}
    identifier = rows.get("identifier")
    fields: List[str] = []
    try:
        if rows.get("fields"):
            fields = json.loads(rows["fields"])
    except json.JSONDecodeError:
        fields = []
    return identifier, fields


def _fetch_search_results(
    name: str,
    url: str,
    headers: dict,
    log_callback=None,
    cancel_token=None,
) -> List[Iterable]:
    first_result = 1
    more_pages_available = True
    results: List[Iterable] = []

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            break
        paginated_url = f"{url}?pageSize=500&firstResult={first_result}"
        json_response, throttle_ms, requests_remaining = send_request(
            paginated_url, headers, cancel_token=cancel_token, log_callback=log_callback
        )
        if cancel_token and cancel_token.is_set():
            break
        if json_response:
            data = json.loads(json_response).get("response", {})
            rows = data.get("results", [])
            results.extend(rows)
            record_api_update(len(rows))
            metadata = data.get("metaData", {})
            log_search_progress(metadata, log_callback)
            more_pages_available = metadata.get("morePagesAvailable", False)
            last_result = metadata.get("lastResult", first_result + 500)
            first_result = last_result + 1
            if should_pause_for_throttle(requests_remaining, throttle_ms):
                log(
                    f"⏳ Throttling after {name}; sleeping {throttle_ms} ms.",
                    log_callback,
                )
                sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)
        else:
            more_pages_available = False

    return results


def _fetch_simple_list(
    name: str,
    url: str,
    headers: dict,
    log_callback=None,
    cancel_token=None,
) -> List[dict]:
    json_response, throttle_ms, requests_remaining = send_request(
        url, headers, cancel_token=cancel_token, log_callback=log_callback
    )
    if cancel_token and cancel_token.is_set():
        return []
    if not json_response:
        log(f"⚠️ No response returned for {name}.", log_callback)
        return []
    try:
        payload = json.loads(json_response).get("response", [])
    except json.JSONDecodeError as exc:
        log(f"⚠️ Failed to parse {name} payload: {exc}", log_callback)
        payload = []
    if isinstance(payload, list):
        record_api_update(len(payload))
    if should_pause_for_throttle(requests_remaining, throttle_ms):
        log(
            f"⏳ Throttling after {name}; sleeping {throttle_ms} ms.",
            log_callback,
        )
        sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)
    return payload if isinstance(payload, list) else []


def sync_product_reference_subset(
    account_name: str,
    db_path: str,
    reference_names=("brands", "categories"),
    log_callback=None,
    cancel_token=None,
) -> Dict[str, int]:
    """Sync selected product lookups without running the full product-import sync."""
    ensure_account_binding(db_path, account_name)
    credentials = fetch_credentials(account_name)
    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
    conn = sqlite3.connect(db_path)
    _ensure_reference_tables(conn)
    cur = conn.cursor()
    results: Dict[str, int] = {}
    try:
        if "brands" in reference_names:
            rows = _fetch_search_results("brands", f"{base}/product-service/brand-search",
                                         credentials.headers, log_callback, cancel_token)
            cur.execute("DELETE FROM ref_brands")
            cur.executemany("INSERT OR REPLACE INTO ref_brands (brandId, brandName) VALUES (?, ?)",
                            ((row[0], row[1]) for row in rows))
            results["brands"] = len(rows)
        if "categories" in reference_names and not (cancel_token and cancel_token.is_set()):
            rows = _fetch_search_results(
                "categories", f"{base}/product-service/brightpearl-category-search",
                credentials.headers, log_callback, cancel_token,
            )
            cur.execute("DELETE FROM ref_product_category")
            cur.executemany(
                "INSERT OR REPLACE INTO ref_product_category VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tuple(row[:8]) for row in rows),
            )
            results["categories"] = len(rows)
        conn.commit()
    finally:
        conn.close()
    for name, count in results.items():
        log(f"✅ Stored {count} rows for {name}.", log_callback)
    return results


def sync_product_reference_data(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> Dict[str, int]:
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return {}

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
    endpoints = {
        "brands": f"{base}/product-service/brand-search",
        "categories": f"{base}/product-service/brightpearl-category-search",
        "options": f"{base}/product-service/option-search",
        "option_values": f"{base}/product-service/option-value-search",
        "types": f"{base}/product-service/product-type-search",
        "seasons": f"{base}/product-service/season/",
    }

    results: Dict[str, int] = {}
    total_steps = 8
    completed_steps = 0

    def bump_progress():
        nonlocal completed_steps
        completed_steps += 1
        pct = int((completed_steps / total_steps) * 100)
        log(f"PROGRESS:{pct}", log_callback)

    log("➡️ Syncing core reference tables...", log_callback)
    fetch_and_store_reference_tables(
        account_name,
        credentials.region,
        credentials.headers,
        db_path,
        log_callback=log_callback,
        cancel_token=cancel_token,
    )
    bump_progress()

    contact_count = _update_supplier_contact_catalogue(
        account_name,
        db_path,
        log_callback=log_callback,
        cancel_token=cancel_token,
    )
    log(f"📇 Synced {contact_count or 0} contacts for supplier lookups.", log_callback)
    bump_progress()

    conn = sqlite3.connect(db_path)
    _ensure_reference_tables(conn)
    cur = conn.cursor()

    log("➡️ Syncing brands...", log_callback)
    brand_rows = _fetch_search_results(
        "brands", endpoints["brands"], credentials.headers, log_callback, cancel_token
    )
    cur.execute("DELETE FROM ref_brands")
    for row in brand_rows:
        if cancel_token and cancel_token.is_set():
            break
        cur.execute(
            "INSERT OR REPLACE INTO ref_brands (brandId, brandName) VALUES (?, ?)",
            (row[0] if len(row) > 0 else None, row[1] if len(row) > 1 else None),
        )
    results["brands"] = len(brand_rows)
    bump_progress()

    log("➡️ Syncing categories...", log_callback)
    category_rows = _fetch_search_results(
        "categories", endpoints["categories"], credentials.headers, log_callback, cancel_token
    )
    cur.execute("DELETE FROM ref_product_category")
    for row in category_rows:
        if cancel_token and cancel_token.is_set():
            break
        cur.execute(
            """
            INSERT OR REPLACE INTO ref_product_category
            (id, name, parentId, active, createdOn, createdById, updatedOn, updatedBy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row[0] if len(row) > 0 else None,
                row[1] if len(row) > 1 else None,
                row[2] if len(row) > 2 else None,
                row[3] if len(row) > 3 else None,
                row[4] if len(row) > 4 else None,
                row[5] if len(row) > 5 else None,
                row[6] if len(row) > 6 else None,
                row[7] if len(row) > 7 else None,
            ),
        )
    results["categories"] = len(category_rows)
    bump_progress()

    log("➡️ Syncing options...", log_callback)
    option_rows = _fetch_search_results(
        "options", endpoints["options"], credentials.headers, log_callback, cancel_token
    )
    cur.execute("DELETE FROM ref_option")
    for row in option_rows:
        if cancel_token and cancel_token.is_set():
            break
        cur.execute(
            "INSERT OR REPLACE INTO ref_option (id, name) VALUES (?, ?)",
            (row[0] if len(row) > 0 else None, row[1] if len(row) > 1 else None),
        )
    results["options"] = len(option_rows)
    bump_progress()

    log("➡️ Syncing option values...", log_callback)
    option_value_rows = _fetch_search_results(
        "option values",
        endpoints["option_values"],
        credentials.headers,
        log_callback,
        cancel_token,
    )
    cur.execute("DELETE FROM ref_option_values")
    for row in option_value_rows:
        if cancel_token and cancel_token.is_set():
            break
        cur.execute(
            """
            INSERT OR REPLACE INTO ref_option_values
            (optionId, optionName, optionValueId, optionValueName)
            VALUES (?, ?, ?, ?)
            """,
            (
                row[0] if len(row) > 0 else None,
                row[1] if len(row) > 1 else None,
                row[2] if len(row) > 2 else None,
                row[3] if len(row) > 3 else None,
            ),
        )
    results["option_values"] = len(option_value_rows)
    bump_progress()

    log("➡️ Syncing types...", log_callback)
    type_rows = _fetch_search_results(
        "types", endpoints["types"], credentials.headers, log_callback, cancel_token
    )
    cur.execute("DELETE FROM ref_type")
    for row in type_rows:
        if cancel_token and cancel_token.is_set():
            break
        cur.execute(
            "INSERT OR REPLACE INTO ref_type (id, name) VALUES (?, ?)",
            (row[0] if len(row) > 0 else None, row[1] if len(row) > 1 else None),
        )
    results["types"] = len(type_rows)
    bump_progress()

    log("➡️ Syncing seasons...", log_callback)
    season_rows = _fetch_simple_list(
        "seasons", endpoints["seasons"], credentials.headers, log_callback, cancel_token
    )
    cur.execute("DELETE FROM ref_season")
    for row in season_rows:
        if cancel_token and cancel_token.is_set():
            break
        cur.execute(
            """
            INSERT OR REPLACE INTO ref_season (id, name, description, dateFrom, dateTo)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row.get("id"),
                row.get("name"),
                row.get("description"),
                row.get("dateFrom"),
                row.get("dateTo"),
            ),
        )
    results["seasons"] = len(season_rows)
    bump_progress()

    conn.commit()
    conn.close()

    return results


def _lookup_single_id(
    cur: sqlite3.Cursor, table: str, id_column: str, name_column: str, value: Optional[str]
) -> Optional[int]:
    if not value:
        return None
    cur.execute(
        f"SELECT {id_column} FROM {table} WHERE lower({name_column}) = lower(?)",
        (value,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _lookup_supplier_contact_id(cur: sqlite3.Cursor, company_name: Optional[str]) -> Optional[int]:
    if not company_name:
        return None
    cur.execute(
        """
        SELECT contactId
        FROM contact_catalogue
        WHERE isSupplier = 1
          AND lower(trim(companyName)) = lower(trim(?))
        ORDER BY contactId ASC
        LIMIT 1
        """,
        (company_name,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _sku_exists_in_catalogue(cur: sqlite3.Cursor, sku: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM product_catalogue
        WHERE lower(trim(SKU)) = lower(trim(?))
        LIMIT 1
        """,
        (sku,),
    )
    return cur.fetchone() is not None


def _find_default_tax_rate_id(payload: object) -> Optional[int]:
    if isinstance(payload, dict):
        if "defaultTaxRateId" in payload:
            try:
                return int(payload["defaultTaxRateId"])
            except (TypeError, ValueError):
                return None
        for value in payload.values():
            nested = _find_default_tax_rate_id(value)
            if nested is not None:
                return nested
    if isinstance(payload, list):
        for item in payload:
            nested = _find_default_tax_rate_id(item)
            if nested is not None:
                return nested
    return None


def _load_default_tax_code(cur: sqlite3.Cursor) -> Tuple[Optional[int], Optional[str]]:
    try:
        cur.execute("SELECT rawJson FROM ref_configuration WHERE id = 1 LIMIT 1")
        row = cur.fetchone()
        if not row or not row[0]:
            return None, None
        config_payload = json.loads(row[0])
        default_tax_rate_id = _find_default_tax_rate_id(config_payload)
        if default_tax_rate_id is None:
            return None, None

        cur.execute(
            "SELECT taxCodeId, code FROM ref_taxcode WHERE taxCodeId = ? LIMIT 1",
            (default_tax_rate_id,),
        )
        tax_row = cur.fetchone()
        if not tax_row:
            return None, None
        tax_code_id = int(tax_row[0]) if tax_row[0] is not None else None
        tax_code = str(tax_row[1]).strip() if tax_row[1] is not None else None
        return tax_code_id, tax_code or None
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return None, None


def validate_product_import_csv(
    csv_path: str,
    db_path: str,
    account_name: str,
    log_callback=None,
    cancel_token=None,
) -> Dict[str, object]:
    ensure_account_binding(db_path, account_name)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    _ensure_reference_tables(conn)
    _ensure_import_tables(conn)

    missing_brands: set[str] = set()
    missing_types: set[str] = set()
    missing_categories: set[str] = set()
    missing_options: set[str] = set()
    missing_option_values: set[Tuple[str, str]] = set()
    missing_seasons: set[str] = set()
    missing_reporting_categories: set[str] = set()
    missing_reporting_subcategories: set[str] = set()
    missing_reporting_seasons: set[str] = set()
    missing_primary_suppliers: set[str] = set()
    missing_suppliers: set[str] = set()

    unmatched_rows: List[dict] = []
    duplicate_rows: List[dict] = []
    missing_required_rows: List[dict] = []
    seen_skus: Dict[str, dict] = {}
    duplicate_skus: set[str] = set()
    default_tax_code_id, default_tax_code = _load_default_tax_code(cur)

    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        headers = reader.fieldnames or []
        option_headers = [
            header
            for header in headers
            if header and header not in BASE_HEADERS and not str(header).strip().startswith("PCF_")
        ]

        if OPTION_PLACEHOLDER in headers:
            log(
                "⚠️ CSV still contains the option placeholder header; "
                "replace it with actual option names.",
                log_callback,
            )

        inserted = 0
        log("⏳ Product import validation started. Processed 0 CSV rows...", log_callback)
        for idx, row in enumerate(reader, 1):
            if cancel_token and cancel_token.is_set():
                log(f"🛑 Cancel at row {idx}. Inserted so far: {inserted}.", log_callback)
                break

            sku = _safe_text(row.get("sku"))
            if not sku:
                continue
            product_name = _safe_text(row.get("productName"))
            length_errors = product_field_length_errors(sku, product_name or "")
            if length_errors:
                missing_required_rows.append(row_with_validation_error(row, length_errors))
                continue
            if sku in seen_skus:
                if sku not in duplicate_skus:
                    first_row = dict(seen_skus[sku])
                    first_row["duplicateReason"] = "Duplicate SKU"
                    duplicate_rows.append(first_row)
                    first_row_unmatched = dict(seen_skus[sku])
                    first_row_unmatched["missingReferences"] = "sku:Duplicate SKU"
                    unmatched_rows.append(first_row_unmatched)
                    duplicate_skus.add(sku)
                row_copy = dict(row)
                row_copy["duplicateReason"] = "Duplicate SKU"
                duplicate_rows.append(row_copy)
                row_unmatched = dict(row)
                row_unmatched["missingReferences"] = "sku:Duplicate SKU"
                unmatched_rows.append(row_unmatched)
                continue
            seen_skus[sku] = dict(row)
            if _sku_exists_in_catalogue(cur, sku):
                row_copy = dict(row)
                row_copy["missingReferences"] = "sku:Already exists in product catalogue"
                unmatched_rows.append(row_copy)
                continue

            brand_name = _safe_text(row.get("brand"))
            type_name = _safe_text(row.get("Type"))
            category_names = _split_list(row.get("category"))
            season_names = _split_list(row.get("seasons"))
            primary_supplier_value = _safe_text(row.get("primarySupplier"))
            supplier_values = _split_pipe_list(row.get("Supplier"))
            supplier_values = list(dict.fromkeys(supplier_values))
            suppliers_joined = "|".join(supplier_values) if supplier_values else None

            reporting_category = _safe_text(row.get("reportingCategory"))
            reporting_subcategory = _safe_text(row.get("reportingSubcategory"))
            reporting_season = _safe_text(row.get("reportingSeason"))
            taxable_value = _safe_bool(row.get("taxable"))
            tax_code_value = _safe_text(row.get("taxCode"))
            if tax_code_value is None and (taxable_value is None or tax_code_value is None):
                if default_tax_code_id is not None:
                    tax_code_value = str(default_tax_code_id)
                elif default_tax_code:
                    tax_code_value = default_tax_code

            brand_id = _lookup_single_id(cur, "ref_brands", "brandId", "brandName", brand_name)
            if brand_name and brand_id is None:
                missing_brands.add(brand_name)

            type_id = _lookup_single_id(cur, "ref_type", "id", "name", type_name)
            if type_name and type_id is None:
                missing_types.add(type_name)

            reporting_category_id = _lookup_single_id(
                cur, "ref_product_category", "id", "name", reporting_category
            )
            if reporting_category and reporting_category_id is None:
                missing_reporting_categories.add(reporting_category)

            reporting_subcategory_id = _lookup_single_id(
                cur, "ref_product_category", "id", "name", reporting_subcategory
            )
            if reporting_subcategory and reporting_subcategory_id is None:
                missing_reporting_subcategories.add(reporting_subcategory)

            reporting_season_id = _lookup_single_id(
                cur, "ref_season", "id", "name", reporting_season
            )
            if reporting_season and reporting_season_id is None:
                missing_reporting_seasons.add(reporting_season)

            primary_supplier_values = _split_pipe_list(primary_supplier_value)
            primary_supplier_name: Optional[str] = None
            primary_supplier_id: Optional[int] = None
            primary_supplier_validation_error: Optional[str] = None
            if len(primary_supplier_values) > 1:
                primary_supplier_validation_error = (
                    "Only one Primary Supplier is allowed per product."
                )
            elif len(primary_supplier_values) == 1:
                primary_supplier_name = primary_supplier_values[0]
                primary_supplier_id = _lookup_supplier_contact_id(cur, primary_supplier_name)
                if primary_supplier_id is None:
                    missing_primary_suppliers.add(primary_supplier_name)

            supplier_contact_ids: List[int] = []
            for supplier_name in supplier_values:
                supplier_contact_id = _lookup_supplier_contact_id(cur, supplier_name)
                if supplier_contact_id is None:
                    missing_suppliers.add(supplier_name)
                    continue
                supplier_contact_ids.append(supplier_contact_id)

            cur.execute(
                """
                INSERT OR REPLACE INTO product_import_core (
                    sku, productName, brand, brandId, productType, productTypeId,
                    ean, upc, isbn, mpn, barcode, stockTracked, weight,
                    dimensions_width, dimensions_length, dimensions_height,
                    taxable, taxCode, productCondition, description_text,
                    shortDescription_text, primarySupplier, primarySupplierId,
                    suppliers, supplierContactIds, nominalCodeStock, nominalCodePurchases,
                    nominalCodeSales, bundleTrueFalse, bundleComponents,
                    reportingCategory, reportingCategoryId, reportingSubcategory,
                    reportingSubcategoryId, reportingSeason, reportingSeasonId
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sku,
                    product_name,
                    brand_name,
                    brand_id,
                    type_name,
                    type_id,
                    _safe_text(row.get("ean")),
                    _safe_text(row.get("upc")),
                    _safe_text(row.get("isbn")),
                    _safe_text(row.get("mpn")),
                    _safe_text(row.get("barcode")),
                    _safe_bool(row.get("stockTracked")),
                    _safe_float(row.get("weight")),
                    _safe_float(row.get("dimensions.width")),
                    _safe_float(row.get("dimensions.length")),
                    _safe_float(row.get("dimensions.height")),
                    taxable_value,
                    tax_code_value,
                    _safe_text(row.get("productCondition")),
                    _safe_text(row.get("description.text")),
                    _safe_text(row.get("shortDescription.text")),
                    primary_supplier_name,
                    primary_supplier_id,
                    suppliers_joined,
                    json.dumps(supplier_contact_ids),
                    _safe_text(row.get("nominalCodeStock")),
                    _safe_text(row.get("nominalCodePurchases")),
                    _safe_text(row.get("nominalCodeSales")),
                    _safe_bool(row.get("bundleTrueFalse")),
                    _safe_text(row.get("bundle_composition")),
                    reporting_category,
                    reporting_category_id,
                    reporting_subcategory,
                    reporting_subcategory_id,
                    reporting_season,
                    reporting_season_id,
                ),
            )

            for category_name in category_names:
                category_id = _lookup_single_id(
                    cur, "ref_product_category", "id", "name", category_name
                )
                if category_id is None:
                    missing_categories.add(category_name)
                cur.execute(
                    """
                    INSERT INTO product_import_category
                    (sku, category_name, category_id)
                    VALUES (?, ?, ?)
                    """,
                    (sku, category_name, category_id),
                )

            for season_name in season_names:
                season_id = _lookup_single_id(cur, "ref_season", "id", "name", season_name)
                if season_id is None:
                    missing_seasons.add(season_name)
                else:
                    cur.execute(
                        """
                        INSERT INTO product_import_seasons
                        (sku, season_name, season_id)
                        VALUES (?, ?, ?)
                        """,
                        (sku, season_name, season_id),
                    )

            inserted_options = set()
            for option_header in option_headers:
                option_name = _safe_text(option_header)
                option_value_name = _safe_text(row.get(option_header))
                if not option_name or not option_value_name:
                    continue
                option_id = _lookup_single_id(cur, "ref_option", "id", "name", option_name)
                if option_id is None:
                    missing_options.add(option_name)
                if (sku, option_name) not in inserted_options:
                    cur.execute(
                        """
                        INSERT INTO product_import_options (sku, option_name, option_id)
                        VALUES (?, ?, ?)
                        """,
                        (sku, option_name, option_id),
                    )
                    inserted_options.add((sku, option_name))

                option_value_id = None
                if option_id is not None:
                    cur.execute(
                        """
                        SELECT optionValueId
                        FROM ref_option_values
                        WHERE optionId = ? AND lower(optionValueName) = lower(?)
                        """,
                        (option_id, option_value_name),
                    )
                    row_value = cur.fetchone()
                    option_value_id = row_value[0] if row_value else None
                else:
                    cur.execute(
                        """
                        SELECT optionValueId, optionId
                        FROM ref_option_values
                        WHERE lower(optionName) = lower(?)
                          AND lower(optionValueName) = lower(?)
                        """,
                        (option_name, option_value_name),
                    )
                    row_value = cur.fetchone()
                    if row_value:
                        option_value_id = row_value[0]
                        option_id = option_id or row_value[1]

                if option_value_id is None:
                    missing_option_values.add((option_name, option_value_name))

                cur.execute(
                    """
                    INSERT INTO product_import_option_values
                    (sku, option_name, option_id, option_value_name, option_value_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (sku, option_name, option_id, option_value_name, option_value_id),
                )

            missing_fields: List[str] = []
            if brand_name and brand_id is None:
                missing_fields.append(f"brand:{brand_name}")
            if type_name and type_id is None:
                missing_fields.append(f"type:{type_name}")
            if missing_categories:
                for missing in sorted(missing_categories):
                    if missing in category_names:
                        missing_fields.append(f"category:{missing}")
            if reporting_category and reporting_category_id is None:
                missing_fields.append(f"reportingCategory:{reporting_category}")
            if reporting_subcategory and reporting_subcategory_id is None:
                missing_fields.append(f"reportingSubcategory:{reporting_subcategory}")
            if reporting_season and reporting_season_id is None:
                missing_fields.append(f"reportingSeason:{reporting_season}")
            if primary_supplier_validation_error:
                missing_fields.append("primarySupplier:Only one Primary Supplier is allowed")
            elif primary_supplier_name and primary_supplier_id is None:
                missing_fields.append(f"primarySupplier:{primary_supplier_name}")
            for supplier_name in supplier_values:
                if supplier_name in missing_suppliers:
                    missing_fields.append(f"supplier:{supplier_name}")
            for season_name in season_names:
                if season_name in missing_seasons:
                    missing_fields.append(f"season:{season_name}")
            for option_name, option_value_name in missing_option_values:
                if option_name in option_headers and _safe_text(row.get(option_name)) == option_value_name:
                    missing_fields.append(f"option:{option_name}:{option_value_name}")

            if missing_fields:
                row_copy = dict(row)
                row_copy["missingReferences"] = "; ".join(missing_fields)
                unmatched_rows.append(row_copy)

            inserted += 1
            if idx == 1 or idx % 250 == 0:
                log(
                    f"⏳ Product import validation processed {idx:,} CSV rows; "
                    f"staged {inserted:,} products so far...",
                    log_callback,
                )

    log(
        f"⏳ Product import validation finished scanning CSV rows; staged {inserted:,} products.",
        log_callback,
    )
    conn.commit()
    conn.close()

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)
    if unmatched_rows and unmatched_dir:
        filename = os.path.join(
            unmatched_dir,
            f"{account_name}_product_import_unmatched.csv",
        )
        fieldnames = list(unmatched_rows[0].keys())
        with open(filename, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(unmatched_rows)
        log(f"⚠️ {len(unmatched_rows)} unmatched rows written to {filename}", log_callback)
    if duplicate_rows and unmatched_dir:
        filename = os.path.join(
            unmatched_dir,
            f"{account_name}_product_import_duplicate_skus.csv",
        )
        fieldnames = list(duplicate_rows[0].keys())
        with open(filename, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(duplicate_rows)
        log(
            f"⚠️ {len(duplicate_rows)} duplicate SKU rows written to {filename}",
            log_callback,
        )
    if missing_required_rows and unmatched_dir:
        filename = os.path.join(
            unmatched_dir,
            f"{account_name}_product_import_missing_required_row.csv",
        )
        fieldnames = [*headers, "validation_error"]
        with open(filename, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(missing_required_rows)
        log(f"⚠️ {len(missing_required_rows)} invalid rows written to {filename}", log_callback)

    return {
        "inserted": inserted,
        "missing_brands": sorted(missing_brands),
        "missing_types": sorted(missing_types),
        "missing_categories": sorted(missing_categories),
        "missing_options": sorted(missing_options),
        "missing_option_values": sorted(missing_option_values),
        "missing_seasons": sorted(missing_seasons),
        "missing_reporting_categories": sorted(missing_reporting_categories),
        "missing_reporting_subcategories": sorted(missing_reporting_subcategories),
        "missing_reporting_seasons": sorted(missing_reporting_seasons),
        "missing_primary_suppliers": sorted(missing_primary_suppliers),
        "missing_suppliers": sorted(missing_suppliers),
        "duplicate_skus": sorted(duplicate_skus),
        "missing_required_rows": len(missing_required_rows),
    }


def _parse_int_header(headers: requests.structures.CaseInsensitiveDict, key: str, default: int = 0) -> int:
    try:
        return int(headers.get(key, default))
    except (TypeError, ValueError):
        return default


def _post_json(
    url: str,
    headers: dict,
    payload: Any,
    *,
    log_callback=None,
    cancel_token=None,
) -> Tuple[bool, Optional[int], int, int]:
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            return False, None, 0, 0
        try:
            if LOG_FULL_PAYLOADS:
                log(
                    "🧾 Payload (debug): "
                    + json.dumps(payload, ensure_ascii=False, default=str),
                    log_callback,
                )
            log_payload(
                "🧾 Payload (full): "
                + json.dumps(payload, ensure_ascii=False, default=str),
                log_callback,
            )
            response = requests.post(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("POST", url, payload, response, log_callback)
            status = response.status_code
            log(f"POST {url} attempt {attempt} → {status}", log_callback)
            log_payload(
                f"📥 Response {status}: {response.text}",
                log_callback,
            )

            if status in (200, 201, 202):
                record_api_update(estimate_record_count(payload))
                created_id: Optional[int] = None
                try:
                    data = response.json()
                except ValueError:
                    data = {}

                if isinstance(data, dict):
                    response_value = data.get("response")
                    if isinstance(response_value, dict):
                        created_id = _safe_int(response_value.get("id"))
                    else:
                        created_id = _safe_int(response_value)

                requests_remaining = _parse_int_header(
                    response.headers, "brightpearl-requests-remaining", 3
                )
                throttle_ms = _parse_int_header(
                    response.headers, "brightpearl-next-throttle-period", 0
                )

                if created_id is None:
                    snippet = json.dumps(data, ensure_ascii=False)[:300]
                    log(
                        "ℹ️ 200-series response but no numeric id parsed. "
                        f"Body snippet: {snippet}",
                        log_callback,
                    )

                return True, created_id, requests_remaining, throttle_ms

            log(f"❌ {status}: {response.text[:400]}", log_callback)

        except requests.RequestException as exc:
            log(f"❌ request error: {exc}", log_callback)

        sleep_ms = default_sleep_ms * attempt
        sleep_with_cancel_ms(sleep_ms, cancel_token, log_callback)

    return False, None, 0, 0


def _parse_int_list_json(raw_value: Optional[str]) -> List[int]:
    if raw_value is None:
        return []
    try:
        loaded = json.loads(raw_value)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    values: List[int] = []
    for item in loaded:
        parsed = _safe_int(item)
        if parsed is not None:
            values.append(parsed)
    return values


def _chunk_list(values: Sequence[int], chunk_size: int) -> List[List[int]]:
    if chunk_size <= 0:
        return [list(values)]
    return [list(values[i : i + chunk_size]) for i in range(0, len(values), chunk_size)]


def _put_json(
    url: str,
    headers: dict,
    payload: dict,
    *,
    log_callback=None,
    cancel_token=None,
) -> Tuple[bool, int, int]:
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            return False, 0, 0
        try:
            if LOG_FULL_PAYLOADS:
                log(
                    "🧾 Payload (debug): "
                    + json.dumps(payload, ensure_ascii=False, default=str),
                    log_callback,
                )
            log_payload(
                "🧾 Payload (full): "
                + json.dumps(payload, ensure_ascii=False, default=str),
                log_callback,
            )
            response = requests.put(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("PUT", url, payload, response, log_callback)
            status = response.status_code
            log(f"PUT {url} attempt {attempt} → {status}", log_callback)
            log_payload(
                f"📥 Response {status}: {response.text}",
                log_callback,
            )

            if status in (200, 201, 202):
                record_api_update(estimate_record_count(payload))
                requests_remaining = _parse_int_header(
                    response.headers, "brightpearl-requests-remaining", 3
                )
                throttle_ms = _parse_int_header(
                    response.headers, "brightpearl-next-throttle-period", 0
                )
                return True, requests_remaining, throttle_ms

            log(f"❌ {status}: {response.text[:400]}", log_callback)
        except requests.RequestException as exc:
            log(f"❌ request error: {exc}", log_callback)

        sleep_ms = default_sleep_ms * attempt
        sleep_with_cancel_ms(sleep_ms, cancel_token, log_callback)

    return False, 0, 0


def _update_missing_brand(
    conn: sqlite3.Connection,
    brand_name: str,
    brand_id: int,
) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO ref_brands (brandId, brandName) VALUES (?, ?)",
        (brand_id, brand_name),
    )
    cur.execute(
        "UPDATE product_import_core SET brandId = ? WHERE brand = ?",
        (brand_id, brand_name),
    )
    conn.commit()


def _update_missing_type(conn: sqlite3.Connection, type_name: str, type_id: int) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO ref_type (id, name) VALUES (?, ?)",
        (type_id, type_name),
    )
    cur.execute(
        "UPDATE product_import_core SET productTypeId = ? WHERE productType = ?",
        (type_id, type_name),
    )
    conn.commit()


def _update_missing_category(
    conn: sqlite3.Connection, category_name: str, category_id: int
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO ref_product_category
        (id, name, parentId, active, createdOn, createdById, updatedOn, updatedBy)
        VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, NULL)
        """,
        (category_id, category_name),
    )
    cur.execute(
        "UPDATE product_import_category SET category_id = ? WHERE category_name = ?",
        (category_id, category_name),
    )
    cur.execute(
        "UPDATE product_import_core SET reportingCategoryId = ? WHERE reportingCategory = ?",
        (category_id, category_name),
    )
    cur.execute(
        "UPDATE product_import_core SET reportingSubcategoryId = ? WHERE reportingSubcategory = ?",
        (category_id, category_name),
    )
    conn.commit()


def _update_missing_option(conn: sqlite3.Connection, option_name: str, option_id: int) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO ref_option (id, name) VALUES (?, ?)",
        (option_id, option_name),
    )
    cur.execute(
        "UPDATE product_import_options SET option_id = ? WHERE option_name = ?",
        (option_id, option_name),
    )
    cur.execute(
        "UPDATE product_import_option_values SET option_id = ? WHERE option_name = ?",
        (option_id, option_name),
    )
    conn.commit()


def _default_product_type_id(cur: sqlite3.Cursor) -> Optional[int]:
    cur.execute("SELECT id FROM ref_type WHERE lower(name) = 'default' LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


def _product_type_ids_for_option(cur: sqlite3.Cursor, option_name: str) -> List[int]:
    cur.execute(
        """
        SELECT DISTINCT core.productTypeId
        FROM product_import_options AS opts
        JOIN product_import_core AS core ON core.sku = opts.sku
        WHERE opts.option_name = ? AND core.productTypeId IS NOT NULL
        """,
        (option_name,),
    )
    return [row[0] for row in cur.fetchall() if row[0] is not None]


def _associate_option_to_product_type(
    base: str,
    headers: dict,
    option_id: int,
    product_type_id: int,
    *,
    log_callback=None,
    cancel_token=None,
) -> None:
    url = f"{base}/product-service/product-type/{product_type_id}/option-association/{option_id}"
    ok, _, requests_remaining, throttle_ms = _post_json(
        url,
        headers,
        {},
        log_callback=log_callback,
        cancel_token=cancel_token,
    )
    if ok:
        log(
            f"🔗 Associated option {option_id} with product type {product_type_id}.",
            log_callback,
        )
    if should_pause_for_throttle(requests_remaining, throttle_ms):
        sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)


def _update_missing_option_value(
    conn: sqlite3.Connection,
    option_id: int,
    option_name: str,
    option_value_name: str,
    option_value_id: int,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO ref_option_values
        (optionId, optionName, optionValueId, optionValueName)
        VALUES (?, ?, ?, ?)
        """,
        (option_id, option_name, option_value_id, option_value_name),
    )
    cur.execute(
        """
        UPDATE product_import_option_values
        SET option_value_id = ?
        WHERE option_name = ? AND option_value_name = ?
        """,
        (option_value_id, option_name, option_value_name),
    )
    conn.commit()


def create_missing_product_references(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> Dict[str, int]:
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return {}

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
    headers = {**credentials.headers, "Content-Type": "application/json"}

    conn = sqlite3.connect(db_path)
    _ensure_reference_tables(conn)
    cur = conn.cursor()
    default_type_id = _default_product_type_id(cur)

    created_counts = {
        "brands": 0,
        "categories": 0,
        "options": 0,
        "option_values": 0,
        "types": 0,
    }

    cur.execute(
        "SELECT DISTINCT brand FROM product_import_core WHERE brand IS NOT NULL AND brandId IS NULL"
    )
    for (brand_name,) in cur.fetchall():
        if cancel_token and cancel_token.is_set():
            break
        payload = {"name": brand_name, "description": f"Imported {brand_name}"}
        url = f"{base}/product-service/brand"
        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok and created_id:
            _update_missing_brand(conn, brand_name, created_id)
            created_counts["brands"] += 1
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    cur.execute(
        "SELECT DISTINCT productType FROM product_import_core "
        "WHERE productType IS NOT NULL AND productTypeId IS NULL"
    )
    for (type_name,) in cur.fetchall():
        if cancel_token and cancel_token.is_set():
            break
        payload = {"name": type_name}
        url = f"{base}/product-service/product-type"
        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok and created_id:
            _update_missing_type(conn, type_name, created_id)
            created_counts["types"] += 1
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    cur.execute(
        """
        SELECT DISTINCT category_name
        FROM product_import_category
        WHERE category_id IS NULL AND category_name IS NOT NULL
        UNION
        SELECT DISTINCT reportingCategory
        FROM product_import_core
        WHERE reportingCategoryId IS NULL AND reportingCategory IS NOT NULL
        UNION
        SELECT DISTINCT reportingSubcategory
        FROM product_import_core
        WHERE reportingSubcategoryId IS NULL AND reportingSubcategory IS NOT NULL
        """
    )
    for (category_name,) in cur.fetchall():
        if cancel_token and cancel_token.is_set():
            break
        payload = {"name": category_name}
        url = f"{base}/product-service/brightpearl-category/"
        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok and created_id:
            _update_missing_category(conn, category_name, created_id)
            created_counts["categories"] += 1
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    cur.execute(
        "SELECT DISTINCT option_name FROM product_import_option_values "
        "WHERE option_name IS NOT NULL AND option_id IS NULL"
    )
    for (option_name,) in cur.fetchall():
        if cancel_token and cancel_token.is_set():
            break
        payload = {"name": option_name}
        url = f"{base}/product-service/option/"
        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok and created_id:
            _update_missing_option(conn, option_name, created_id)
            created_counts["options"] += 1
            type_ids = set(_product_type_ids_for_option(cur, option_name))
            if default_type_id is not None:
                type_ids.add(default_type_id)
            for product_type_id in sorted(type_ids):
                if cancel_token and cancel_token.is_set():
                    break
                _associate_option_to_product_type(
                    base,
                    headers,
                    created_id,
                    product_type_id,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    cur.execute(
        """
        SELECT DISTINCT option_name, option_value_name, option_id
        FROM product_import_option_values
        WHERE option_value_id IS NULL AND option_value_name IS NOT NULL
        """
    )
    for option_name, option_value_name, option_id in cur.fetchall():
        if cancel_token and cancel_token.is_set():
            break
        if option_id is None:
            continue
        payload = {"optionValueName": option_value_name}
        url = f"{base}/product-service/option/{option_id}/value"
        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok and created_id:
            _update_missing_option_value(
                conn, option_id, option_name, option_value_name, created_id
            )
            created_counts["option_values"] += 1
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    conn.close()
    return created_counts


def _parse_bundle_components(
    raw_components: Optional[str],
) -> List[Tuple[str, float]]:
    if not raw_components:
        return []
    components = []
    for chunk in str(raw_components).split("|"):
        if not chunk.strip():
            continue
        if ":" not in chunk:
            continue
        sku, qty = chunk.split(":", 1)
        sku = sku.strip()
        qty_value = _safe_float(qty)
        if sku and qty_value is not None:
            components.append((sku, qty_value))
    return components


def _build_product_payload(
    row: sqlite3.Row,
    categories: List[int],
    seasons: List[int],
    variations: List[Tuple[int, int]],
    bundle_components: List[Tuple[int, float]],
) -> dict:
    payload: Dict[str, object] = {}

    if row["brandId"]:
        payload["brandId"] = int(row["brandId"])
    if row["productTypeId"]:
        payload["productTypeId"] = int(row["productTypeId"])

    identity = {"sku": row["sku"]}
    if row["ean"]:
        identity["ean"] = row["ean"]
    if row["upc"]:
        identity["upc"] = row["upc"]
    if row["isbn"]:
        identity["isbn"] = row["isbn"]
    if row["mpn"]:
        identity["mpn"] = row["mpn"]
    if row["barcode"]:
        identity["barcode"] = row["barcode"]
    if identity:
        payload["identity"] = identity

    stock = {}
    if row["stockTracked"] is not None:
        stock["stockTracked"] = bool(row["stockTracked"])
    if row["weight"] is not None:
        stock["weight"] = {"magnitude": row["weight"]}
    if stock:
        payload["stock"] = stock

    dimensions = {}
    if row["dimensions_width"] is not None:
        dimensions["width"] = row["dimensions_width"]
    if row["dimensions_length"] is not None:
        dimensions["length"] = row["dimensions_length"]
    if row["dimensions_height"] is not None:
        dimensions["height"] = row["dimensions_height"]
    if dimensions:
        payload["dimensions"] = dimensions

    financial = {}
    if row["taxable"] is not None:
        financial["taxable"] = bool(row["taxable"])
    tax_code = row["taxCode"]
    if tax_code:
        if str(tax_code).isdigit():
            financial["taxCode"] = {"id": int(tax_code)}
        else:
            financial["taxCode"] = {"code": tax_code}
    if financial:
        payload["financialDetails"] = financial

    channel = {
        "salesChannelName": "Brightpearl",
    }
    if row["productName"]:
        channel["productName"] = row["productName"]
    if row["productCondition"]:
        channel["productCondition"] = row["productCondition"]
    if categories:
        channel["categories"] = [
            {"categoryCode": str(category_id)} for category_id in categories
        ]
    if row["description_text"]:
        channel["description"] = {
            "languageCode": "en",
            "text": row["description_text"],
            "format": "HTML_FRAGMENT",
        }
    if row["shortDescription_text"]:
        channel["shortDescription"] = {
            "languageCode": "en",
            "text": row["shortDescription_text"],
            "format": "HTML_FRAGMENT",
        }
    payload["salesChannels"] = [channel]

    if variations:
        payload["variations"] = [
            {"optionId": option_id, "optionValueId": option_value_id}
            for option_id, option_value_id in variations
        ]

    if seasons:
        payload["seasonIds"] = seasons

    if row["nominalCodeStock"]:
        payload["nominalCodeStock"] = row["nominalCodeStock"]
    if row["nominalCodePurchases"]:
        payload["nominalCodePurchases"] = row["nominalCodePurchases"]
    if row["nominalCodeSales"]:
        payload["nominalCodeSales"] = row["nominalCodeSales"]

    reporting = {}
    if row["reportingSeasonId"]:
        reporting["seasonId"] = int(row["reportingSeasonId"])
    if row["reportingCategoryId"]:
        reporting["categoryId"] = int(row["reportingCategoryId"])
    if row["reportingSubcategoryId"]:
        reporting["subcategoryId"] = int(row["reportingSubcategoryId"])
    if reporting:
        payload["reporting"] = reporting

    if row["bundleTrueFalse"]:
        if bundle_components:
            payload["composition"] = {
                "bundle": True,
                "bundleComponents": [
                    {"productId": pid, "productQuantity": qty}
                    for pid, qty in bundle_components
                ],
            }
        else:
            payload["composition"] = {"bundle": True, "bundleComponents": []}

    return payload


def _ensure_product_catalogue_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
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
    conn.commit()


def _upsert_product_catalogue_row(
    conn: sqlite3.Connection,
    product_id: int,
    row: sqlite3.Row,
    payload: dict,
) -> None:
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    identity = payload.get("identity", {}) if isinstance(payload, dict) else {}
    stock = payload.get("stock", {}) if isinstance(payload, dict) else {}
    sales_channels = payload.get("salesChannels", []) if isinstance(payload, dict) else []
    first_channel = sales_channels[0] if sales_channels else {}

    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO product_catalogue (
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
            product_id,
            row["productName"] or identity.get("productName"),
            row["sku"],
            identity.get("barcode"),
            identity.get("ean"),
            identity.get("upc"),
            identity.get("isbn"),
            identity.get("mpn"),
            int(bool(stock.get("stockTracked"))) if stock.get("stockTracked") is not None else None,
            first_channel.get("salesChannelName", "Brightpearl"),
            now_iso,
            now_iso,
            None,
            None,
            row["brandId"],
            row["productTypeId"],
            None,
            None,
        ),
    )
    conn.commit()


def create_products_from_import(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
    headers = {**credentials.headers, "Content-Type": "application/json"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    _ensure_product_catalogue_table(conn)
    ensure_processing_columns(conn, "product_import_core")

    cur.execute(f"SELECT * FROM product_import_core WHERE {unprocessed_where_clause()}")
    rows = cur.fetchall()
    total = len(rows)
    created = 0
    primary_supplier_product_map: Dict[int, List[int]] = {}
    suppliers_by_product_id: Dict[int, List[int]] = {}

    for idx, row in enumerate(rows, 1):
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Product creation cancelled at {idx - 1}/{total}.", log_callback)
            break

        cur.execute(
            "SELECT category_id FROM product_import_category WHERE sku = ? AND category_id IS NOT NULL",
            (row["sku"],),
        )
        categories = [r[0] for r in cur.fetchall() if r[0] is not None]

        cur.execute(
            "SELECT season_id FROM product_import_seasons WHERE sku = ? AND season_id IS NOT NULL",
            (row["sku"],),
        )
        seasons = [r[0] for r in cur.fetchall() if r[0] is not None]

        cur.execute(
            """
            SELECT option_id, option_value_id
            FROM product_import_option_values
            WHERE sku = ? AND option_id IS NOT NULL AND option_value_id IS NOT NULL
            """,
            (row["sku"],),
        )
        variations = [(r[0], r[1]) for r in cur.fetchall()]

        bundle_components = []
        for component_sku, qty in _parse_bundle_components(row["bundleComponents"]):
            cur.execute(
                "SELECT productId FROM product_catalogue WHERE SKU = ?",
                (component_sku,),
            )
            component_row = cur.fetchone()
            if component_row:
                bundle_components.append((component_row[0], qty))
            else:
                log(
                    f"⚠️ Bundle component SKU {component_sku} not found in catalogue.",
                    log_callback,
                )

        payload = _build_product_payload(row, categories, seasons, variations, bundle_components)
        url = f"{base}/product-service/product"

        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )

        if ok:
            mark_table_key_processed(conn, "product_import_core", "sku", row["sku"], f"Created product {created_id or row['sku']}")
            conn.commit()
            created += 1
            if created_id:
                log(f"✅ Created product {row['sku']} (id {created_id}).", log_callback)
                _upsert_product_catalogue_row(conn, created_id, row, payload)
                primary_supplier_id = _safe_int(row["primarySupplierId"])
                if primary_supplier_id is not None:
                    primary_supplier_product_map.setdefault(primary_supplier_id, []).append(
                        created_id
                    )
                supplier_ids = _parse_int_list_json(row["supplierContactIds"])
                if supplier_ids:
                    suppliers_by_product_id[created_id] = supplier_ids
            else:
                log(f"✅ Created product {row['sku']}.", log_callback)
        else:
            log(f"❌ Failed to create product {row['sku']}.", log_callback)

        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

        progress = int((idx / max(total, 1)) * 100)
        log(f"PROGRESS:{progress}", log_callback)

    for primary_supplier_id, product_ids in primary_supplier_product_map.items():
        if cancel_token and cancel_token.is_set():
            break
        if not product_ids:
            continue
        url = f"{base}/product-service/primary-supplier/{primary_supplier_id}/product"
        unique_product_ids = sorted(set(product_ids))
        product_id_chunks = _chunk_list(unique_product_ids, 500)
        for chunk_index, product_id_chunk in enumerate(product_id_chunks, 1):
            if cancel_token and cancel_token.is_set():
                break
            payload = {"productIds": product_id_chunk}
            ok, requests_remaining, throttle_ms = _put_json(
                url,
                headers,
                payload,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if ok:
                log(
                    "✅ Assigned primary supplier "
                    f"{primary_supplier_id} to {len(product_id_chunk)} product(s) "
                    f"(batch {chunk_index}/{len(product_id_chunks)}).",
                    log_callback,
                )
            else:
                log(
                    "❌ Failed assigning primary supplier "
                    f"{primary_supplier_id} in batch {chunk_index}/{len(product_id_chunks)}.",
                    log_callback,
                )
            if should_pause_for_throttle(requests_remaining, throttle_ms):
                sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    for product_id, supplier_ids in suppliers_by_product_id.items():
        if cancel_token and cancel_token.is_set():
            break
        if not supplier_ids:
            continue
        url = f"{base}/product-service/product/{product_id}/supplier"
        payload = sorted(set(supplier_ids))
        ok, _, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok:
            log(
                f"✅ Assigned {len(payload)} supplier(s) to product {product_id}.",
                log_callback,
            )
        else:
            log(f"❌ Failed assigning suppliers to product {product_id}.", log_callback)
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    conn.close()
    return created


def export_product_id_sku_reference(db_path: str, output_path: str) -> int:
    conn = sqlite3.connect(db_path)
    _ensure_product_catalogue_table(conn)
    cur = conn.cursor()
    cur.execute("SELECT productId, SKU FROM product_catalogue ORDER BY productId")
    rows = cur.fetchall()
    conn.close()

    with open_table(output_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["productId", "sku"])
        writer.writerows(rows)
    return len(rows)


def _product_type_ids_for_update_option(cur: sqlite3.Cursor, option_name: str) -> List[int]:
    cur.execute(
        """
        SELECT DISTINCT catalogue.productTypeId
        FROM product_update_options AS updates
        JOIN product_catalogue AS catalogue ON catalogue.productId = updates.productId
        WHERE updates.option_name = ? AND catalogue.productTypeId IS NOT NULL
        """,
        (option_name,),
    )
    return [row[0] for row in cur.fetchall() if row[0] is not None]


def _update_missing_update_option(
    conn: sqlite3.Connection, option_name: str, option_id: int
) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO ref_option (id, name) VALUES (?, ?)",
        (option_id, option_name),
    )
    cur.execute(
        "UPDATE product_update_options SET option_id = ? WHERE option_name = ?",
        (option_id, option_name),
    )
    cur.execute(
        "UPDATE product_update_option_values SET option_id = ? WHERE option_name = ?",
        (option_id, option_name),
    )
    conn.commit()


def _update_missing_update_option_value(
    conn: sqlite3.Connection,
    option_id: int,
    option_name: str,
    option_value_name: str,
    option_value_id: int,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO ref_option_values
        (optionId, optionName, optionValueId, optionValueName)
        VALUES (?, ?, ?, ?)
        """,
        (option_id, option_name, option_value_id, option_value_name),
    )
    cur.execute(
        """
        UPDATE product_update_option_values
        SET option_value_id = ?
        WHERE option_name = ? AND option_value_name = ?
        """,
        (option_value_id, option_name, option_value_name),
    )
    conn.commit()


def create_missing_product_update_references(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> Dict[str, int]:
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return {}

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
    headers = {**credentials.headers, "Content-Type": "application/json"}

    conn = sqlite3.connect(db_path)
    _ensure_reference_tables(conn)
    _ensure_product_update_tables(conn)
    cur = conn.cursor()
    default_type_id = _default_product_type_id(cur)

    created_counts = {
        "options": 0,
        "option_values": 0,
    }

    cur.execute(
        "SELECT DISTINCT option_name FROM product_update_option_values "
        "WHERE option_name IS NOT NULL AND option_id IS NULL"
    )
    for (option_name,) in cur.fetchall():
        if cancel_token and cancel_token.is_set():
            break
        payload = {"name": option_name}
        url = f"{base}/product-service/option/"
        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok and created_id:
            _update_missing_update_option(conn, option_name, created_id)
            created_counts["options"] += 1
            type_ids = set(_product_type_ids_for_update_option(cur, option_name))
            if default_type_id is not None:
                type_ids.add(default_type_id)
            for product_type_id in sorted(type_ids):
                if cancel_token and cancel_token.is_set():
                    break
                _associate_option_to_product_type(
                    base,
                    headers,
                    created_id,
                    product_type_id,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    cur.execute(
        """
        SELECT DISTINCT option_name, option_value_name, option_id
        FROM product_update_option_values
        WHERE option_value_id IS NULL AND option_value_name IS NOT NULL
        """
    )
    for option_name, option_value_name, option_id in cur.fetchall():
        if cancel_token and cancel_token.is_set():
            break
        if option_id is None:
            continue
        payload = {"optionValueName": option_value_name}
        url = f"{base}/product-service/option/{option_id}/value"
        ok, created_id, requests_remaining, throttle_ms = _post_json(
            url,
            headers,
            payload,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        if ok and created_id:
            _update_missing_update_option_value(
                conn, option_id, option_name, option_value_name, created_id
            )
            created_counts["option_values"] += 1
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    conn.close()
    return created_counts


def create_product_update_template(
    output_path: str, identifier: str, selected_fields: Sequence[str]
) -> None:
    if identifier not in {"productId", "sku"}:
        raise ValueError("Identifier must be either 'productId' or 'sku'.")
    headers = [identifier] + list(selected_fields)
    with open_table(output_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)


def _compress_id_ranges(ids: Sequence[int]) -> str:
    if not ids:
        return ""
    sorted_ids = sorted(set(ids))
    ranges: List[str] = []
    start = prev = sorted_ids[0]
    for current in sorted_ids[1:]:
        if current == prev + 1:
            prev = current
            continue
        if start == prev:
            ranges.append(str(start))
        else:
            ranges.append(f"{start}-{prev}")
        start = prev = current
    if start == prev:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{prev}")
    return ",".join(ranges)


def _fetch_option_uris(
    url: str,
    headers: dict,
    *,
    log_callback=None,
    cancel_token=None,
) -> List[str]:
    if cancel_token and cancel_token.is_set():
        return []
    try:
        response = requests.options(url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        payload = response.json().get("response", {})
        uris = payload.get("getUris", [])
        if isinstance(uris, list):
            return [uri for uri in uris if isinstance(uri, str)]
    except requests.RequestException as exc:
        log(f"❌ OPTIONS request failed: {exc}", log_callback)
    except ValueError as exc:
        log(f"❌ Failed to parse OPTIONS response: {exc}", log_callback)
    return []


def sync_product_update_catalogue(
    account_name: str,
    db_path: str,
    product_ids: Sequence[int],
    log_callback=None,
    cancel_token=None,
) -> int:
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    if not product_ids:
        log("⚠️ No product ids provided for update sync.", log_callback)
        return 0

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/product-service"
    headers = {**credentials.headers, "Content-Type": "application/json"}

    id_set = _compress_id_ranges([int(pid) for pid in product_ids if pid is not None])
    if not id_set:
        log("⚠️ No valid product ids provided for update sync.", log_callback)
        return 0

    option_url = f"{base}/product/{id_set}"
    uris = _fetch_option_uris(option_url, headers, log_callback=log_callback, cancel_token=cancel_token)
    if not uris:
        uris = [f"/product/{id_set}"]

    conn = sqlite3.connect(db_path)
    _ensure_product_update_tables(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM product_catalogue_core")
    cur.execute("DELETE FROM product_catalogue_category")
    cur.execute("DELETE FROM product_catalogue_options")
    cur.execute("DELETE FROM product_catalogue_seasons")
    cur.execute("DELETE FROM product_catalogue_bundle")
    conn.commit()

    total_synced = 0
    total_uris = max(len(uris), 1)
    for idx, uri in enumerate(uris, 1):
        if cancel_token and cancel_token.is_set():
            break
        url = f"{base}{uri}"
        json_response, throttle_ms, requests_remaining = send_request(
            url, headers, cancel_token=cancel_token, log_callback=log_callback
        )
        if cancel_token and cancel_token.is_set():
            break
        if not json_response:
            continue
        try:
            payload = json.loads(json_response).get("response", [])
        except json.JSONDecodeError as exc:
            log(f"⚠️ Failed to parse product payload: {exc}", log_callback)
            continue
        if not isinstance(payload, list):
            continue

        for product in payload:
            product_id = product.get("id")
            if product_id is None:
                continue
            identity = product.get("identity", {}) or {}
            sku = identity.get("sku")
            sales_channel = None
            for channel in product.get("salesChannels", []) or []:
                if channel.get("salesChannelName") == "Brightpearl":
                    sales_channel = channel
                    break
            if sales_channel is None:
                channels = product.get("salesChannels", []) or []
                sales_channel = channels[0] if channels else {}
            description = sales_channel.get("description", {}) if isinstance(sales_channel, dict) else {}
            short_description = (
                sales_channel.get("shortDescription", {}) if isinstance(sales_channel, dict) else {}
            )

            cur.execute(
                """
                INSERT OR REPLACE INTO product_catalogue_core (
                    productId,
                    sku,
                    brandId,
                    productTypeId,
                    collectionId,
                    productGroupId,
                    salesChannelName,
                    productName,
                    productCondition,
                    description_text,
                    shortDescription_text,
                    nominalCodeStock,
                    nominalCodePurchases,
                    nominalCodeSales,
                    reportingCategoryId,
                    reportingSubcategoryId,
                    reportingSeasonId,
                    bundleFlag
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    product_id,
                    sku,
                    product.get("brandId"),
                    product.get("productTypeId"),
                    product.get("collectionId"),
                    product.get("productGroupId"),
                    sales_channel.get("salesChannelName") if isinstance(sales_channel, dict) else None,
                    sales_channel.get("productName") if isinstance(sales_channel, dict) else None,
                    sales_channel.get("productCondition") if isinstance(sales_channel, dict) else None,
                    description.get("text") if isinstance(description, dict) else None,
                    short_description.get("text") if isinstance(short_description, dict) else None,
                    product.get("nominalCodeStock"),
                    product.get("nominalCodePurchases"),
                    product.get("nominalCodeSales"),
                    (product.get("reporting", {}) or {}).get("categoryId"),
                    (product.get("reporting", {}) or {}).get("subcategoryId"),
                    (product.get("reporting", {}) or {}).get("seasonId"),
                    int(bool((product.get("composition", {}) or {}).get("bundle"))),
                ),
            )

            categories = []
            if isinstance(sales_channel, dict):
                categories = sales_channel.get("categories", []) or []
            for category in categories:
                category_code = category.get("categoryCode") if isinstance(category, dict) else None
                if category_code is not None:
                    cur.execute(
                        """
                        INSERT INTO product_catalogue_category (productId, categoryCode)
                        VALUES (?, ?)
                        """,
                        (product_id, str(category_code)),
                    )

            for variation in product.get("variations", []) or []:
                option_id = variation.get("optionId")
                option_value_id = variation.get("optionValueId")
                if option_id is not None and option_value_id is not None:
                    cur.execute(
                        """
                        INSERT INTO product_catalogue_options (productId, optionId, optionValueId)
                        VALUES (?, ?, ?)
                        """,
                        (product_id, option_id, option_value_id),
                    )

            for season_id in product.get("seasonIds", []) or []:
                if season_id is not None:
                    cur.execute(
                        """
                        INSERT INTO product_catalogue_seasons (productId, seasonId)
                        VALUES (?, ?)
                        """,
                        (product_id, season_id),
                    )

            composition = product.get("composition", {}) or {}
            for component in composition.get("bundleComponents", []) or []:
                component_id = component.get("productId")
                component_qty = component.get("productQuantity")
                if component_id is not None and component_qty is not None:
                    cur.execute(
                        """
                        INSERT INTO product_catalogue_bundle (productId, componentProductId, componentQty)
                        VALUES (?, ?, ?)
                        """,
                        (product_id, component_id, component_qty),
                    )

            total_synced += 1

        conn.commit()

        if should_pause_for_throttle(requests_remaining, throttle_ms):
            log(f"⏳ Throttling: waiting {throttle_ms} ms...", log_callback)
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

        progress = int((idx / total_uris) * 100)
        log(f"PROGRESS:{progress}", log_callback)

    conn.close()
    return total_synced


def validate_product_update_csv(
    csv_path: str,
    db_path: str,
    account_name: str,
    log_callback=None,
    cancel_token=None,
) -> Dict[str, object]:
    ensure_account_binding(db_path, account_name)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_reference_tables(conn)
    _ensure_product_catalogue_table(conn)
    _ensure_product_update_tables(conn)
    cur = conn.cursor()
    cur.execute("DELETE FROM product_update_options")
    cur.execute("DELETE FROM product_update_option_values")
    conn.commit()

    header_aliases = {
        "salesChannels.categories.categoryCode": "salesChannels.categories",
    }

    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        headers = reader.fieldnames or []
        if not headers:
            raise ValueError("CSV contains no headers.")
        normalized_headers = [
            header_aliases.get((header or "").strip(), (header or "").strip())
            for header in headers
        ]
        identifier = (normalized_headers[0] or "").strip()
        if identifier not in {"productId", "sku"}:
            raise ValueError("First column must be either productId or sku.")

        update_fields = [header.strip() for header in normalized_headers[1:] if header]
        known_fields = set(PRODUCT_UPDATE_FIELDS)
        option_headers = [
            header for header in normalized_headers[1:] if header and header not in known_fields
        ]
        if identifier == "sku" and "identity.sku" in update_fields:
            raise ValueError("Updating SKU requires productId as the identifier.")

        if OPTION_PLACEHOLDER in option_headers:
            log(
                "⚠️ CSV still contains the option placeholder header; "
                "replace it with actual option names.",
                log_callback,
            )

        if option_headers and "variations" not in update_fields:
            update_fields.append("variations")

        rows = list(reader)
        if header_aliases:
            normalized_rows: List[dict] = []
            for row in rows:
                normalized_row: dict = {}
                for key, value in row.items():
                    if key is None:
                        continue
                    normalized_key = header_aliases.get(key.strip(), key.strip())
                    existing = normalized_row.get(normalized_key)
                    if existing and _safe_text(value):
                        normalized_row[normalized_key] = f"{existing}|{value}"
                    elif normalized_key not in normalized_row:
                        normalized_row[normalized_key] = value
                normalized_rows.append(normalized_row)
            rows = normalized_rows

    seen_identifiers: set[str] = set()
    invalid_rows: List[dict] = []
    inserted = 0
    missing_options: set[str] = set()
    missing_option_values: set[Tuple[str, str]] = set()
    duplicate_variations = 0

    sku_map: Dict[str, int] = {}
    if identifier == "sku":
        skus = []
        for row in rows:
            raw_identifier = _safe_text(row.get(identifier))
            if raw_identifier:
                skus.append(raw_identifier)
        if skus:
            placeholder = ",".join("?" for _ in skus)
            cur.execute(
                f"SELECT SKU, productId FROM product_catalogue WHERE SKU IN ({placeholder})",
                skus,
            )
            sku_map = {row[0]: row[1] for row in cur.fetchall()}

    product_ids: List[int] = []
    for row in rows:
        raw_identifier = _safe_text(row.get(identifier))
        if raw_identifier:
            if identifier == "productId":
                product_id = _safe_int(raw_identifier)
            else:
                product_id = sku_map.get(raw_identifier)
            if product_id is not None:
                product_ids.append(product_id)

    if product_ids:
        sync_count = sync_product_update_catalogue(
            account_name,
            db_path,
            product_ids,
            log_callback=log_callback,
            cancel_token=cancel_token,
        )
        log(f"📦 Synced {sync_count} products for update validation.", log_callback)

    cur.execute("DROP TABLE IF EXISTS validated_product_update")
    column_defs = [_quote_identifier("id") + " INTEGER PRIMARY KEY AUTOINCREMENT", _quote_identifier("productId") + " INTEGER", _quote_identifier("sku") + " TEXT"]
    for field in update_fields:
        column_defs.append(_quote_identifier(field) + " TEXT")
    column_defs.append(processing_column_definitions(include_leading_comma=False))
    cur.execute(f"CREATE TABLE validated_product_update ({', '.join(column_defs)})")
    conn.commit()

    for row in rows:
        if cancel_token and cancel_token.is_set():
            break
        raw_identifier = _safe_text(row.get(identifier))
        issues: List[str] = []
        row_missing_options: set[str] = set()
        row_missing_option_values: set[Tuple[str, str]] = set()
        if not raw_identifier:
            issues.append("Missing identifier value.")
        if identifier == "sku" and raw_identifier and len(raw_identifier) > SKU_MAX_LENGTH:
            issues.append(f"SKU exceeds {SKU_MAX_LENGTH} characters.")
        if raw_identifier and raw_identifier in seen_identifiers:
            issues.append("Duplicate identifier value.")
        if raw_identifier:
            seen_identifiers.add(raw_identifier)

        product_id = None
        sku_value = None
        if identifier == "productId":
            product_id = _safe_int(raw_identifier)
            if product_id is None:
                issues.append("Invalid productId.")
            else:
                cur.execute("SELECT SKU FROM product_catalogue WHERE productId = ?", (product_id,))
                result = cur.fetchone()
                sku_value = result[0] if result else None
                if sku_value is None:
                    issues.append("productId not found in catalogue.")
        else:
            sku_value = raw_identifier
            product_id = sku_map.get(sku_value)
            if product_id is None:
                issues.append("SKU not found in catalogue.")

        brand_id_value = None
        if "brandId" in update_fields:
            raw_brand = _safe_text(row.get("brandId"))
            if raw_brand:
                brand_exists = False
                if raw_brand.isdigit():
                    brand_id_value = _safe_int(raw_brand)
                    cur.execute("SELECT 1 FROM ref_brands WHERE brandId = ?", (brand_id_value,))
                    brand_exists = cur.fetchone() is not None
                else:
                    cur.execute(
                        "SELECT brandId FROM ref_brands WHERE lower(brandName) = lower(?)",
                        (raw_brand,),
                    )
                    result = cur.fetchone()
                    brand_id_value = result[0] if result else None
                    brand_exists = brand_id_value is not None
                if brand_id_value is None or not brand_exists:
                    issues.append(f"Unknown brandId {raw_brand}.")

        product_type_id_value = None
        if "productTypeId" in update_fields:
            raw_type = _safe_text(row.get("productTypeId"))
            if raw_type:
                type_exists = False
                if raw_type.isdigit():
                    product_type_id_value = _safe_int(raw_type)
                    cur.execute("SELECT 1 FROM ref_type WHERE id = ?", (product_type_id_value,))
                    type_exists = cur.fetchone() is not None
                else:
                    cur.execute(
                        "SELECT id FROM ref_type WHERE lower(name) = lower(?)",
                        (raw_type,),
                    )
                    result = cur.fetchone()
                    product_type_id_value = result[0] if result else None
                    type_exists = product_type_id_value is not None
                if product_type_id_value is None or not type_exists:
                    issues.append(f"Unknown productTypeId {raw_type}.")

        primary_supplier_id_value: Optional[int] = None
        if "primarySupplier" in update_fields:
            primary_supplier_names = _split_pipe_list(row.get("primarySupplier"))
            if len(primary_supplier_names) > 1:
                issues.append("Only one primarySupplier value is allowed.")
            elif len(primary_supplier_names) == 1:
                primary_supplier_id_value = _lookup_supplier_contact_id(
                    cur, primary_supplier_names[0]
                )
                if primary_supplier_id_value is None:
                    issues.append(f"Unknown primarySupplier {primary_supplier_names[0]}.")

        supplier_id_values: List[int] = []
        if "Supplier" in update_fields:
            supplier_names = list(dict.fromkeys(_split_pipe_list(row.get("Supplier"))))
            for supplier_name in supplier_names:
                supplier_id = _lookup_supplier_contact_id(cur, supplier_name)
                if supplier_id is None:
                    issues.append(f"Unknown Supplier {supplier_name}.")
                    continue
                supplier_id_values.append(supplier_id)

        category_values = []
        if "salesChannels.categories" in update_fields:
            raw_categories = _split_list(row.get("salesChannels.categories"))
            for category in raw_categories:
                category_id = None
                if category.isdigit():
                    category_id = int(category)
                    cur.execute("SELECT 1 FROM ref_product_category WHERE id = ?", (category_id,))
                else:
                    cur.execute(
                        "SELECT id FROM ref_product_category WHERE lower(name) = lower(?)",
                        (category,),
                    )
                    result = cur.fetchone()
                    category_id = result[0] if result else None
                if category_id is None:
                    issues.append(f"Unknown category {category}.")
                else:
                    category_values.append(str(category_id))

        variations = []
        if "variations" in update_fields:
            variations = _parse_variations(row.get("variations"))
            for option_id, option_value_id in variations:
                cur.execute("SELECT 1 FROM ref_option WHERE id = ?", (option_id,))
                if not cur.fetchone():
                    issues.append(f"Unknown optionId {option_id}.")
                cur.execute(
                    """
                    SELECT 1 FROM ref_option_values
                    WHERE optionId = ? AND optionValueId = ?
                    """,
                    (option_id, option_value_id),
                )
                if not cur.fetchone():
                    issues.append(f"Unknown option value {option_id}:{option_value_id}.")

        option_variations: List[Tuple[int, int]] = []
        inserted_options = set()
        for option_header in option_headers:
            option_name = _safe_text(option_header)
            option_value_name = _safe_text(row.get(option_header))
            if not option_name or not option_value_name:
                continue
            option_id = _lookup_single_id(cur, "ref_option", "id", "name", option_name)
            if option_id is None:
                missing_options.add(option_name)
                row_missing_options.add(option_name)
            if (product_id, option_name) not in inserted_options and product_id is not None:
                cur.execute(
                    """
                    INSERT INTO product_update_options (productId, option_name, option_id)
                    VALUES (?, ?, ?)
                    """,
                    (product_id, option_name, option_id),
                )
                inserted_options.add((product_id, option_name))

            option_value_id = None
            if option_id is not None:
                cur.execute(
                    """
                    SELECT optionValueId
                    FROM ref_option_values
                    WHERE optionId = ? AND lower(optionValueName) = lower(?)
                    """,
                    (option_id, option_value_name),
                )
                row_value = cur.fetchone()
                option_value_id = row_value[0] if row_value else None
            else:
                cur.execute(
                    """
                    SELECT optionValueId, optionId
                    FROM ref_option_values
                    WHERE lower(optionName) = lower(?)
                      AND lower(optionValueName) = lower(?)
                    """,
                    (option_name, option_value_name),
                )
                row_value = cur.fetchone()
                if row_value:
                    option_value_id = row_value[0]
                    option_id = option_id or row_value[1]

            if option_value_id is None:
                missing_option_values.add((option_name, option_value_name))
                row_missing_option_values.add((option_name, option_value_name))

            if product_id is not None:
                cur.execute(
                    """
                    INSERT INTO product_update_option_values
                    (productId, option_name, option_id, option_value_name, option_value_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (product_id, option_name, option_id, option_value_name, option_value_id),
                )

            if option_id is not None and option_value_id is not None:
                option_variations.append((option_id, option_value_id))

        combined_variations = variations + option_variations

        season_values = []
        if "seasonIds" in update_fields:
            raw_seasons = _split_list(row.get("seasonIds"))
            for season in raw_seasons:
                season_id = None
                if season.isdigit():
                    season_id = int(season)
                    cur.execute("SELECT 1 FROM ref_season WHERE id = ?", (season_id,))
                else:
                    cur.execute(
                        "SELECT id FROM ref_season WHERE lower(name) = lower(?)",
                        (season,),
                    )
                    result = cur.fetchone()
                    season_id = result[0] if result else None
                if season_id is None:
                    issues.append(f"Unknown seasonId {season}.")
                else:
                    season_values.append(season_id)

        bundle_components = []
        if "composition.bundleComponents" in update_fields:
            bundle_components = _parse_components_by_id(row.get("composition.bundleComponents"))
            for component_id, _qty in bundle_components:
                cur.execute("SELECT 1 FROM product_catalogue WHERE productId = ?", (component_id,))
                if not cur.fetchone():
                    issues.append(f"Unknown bundle component productId {component_id}.")

        if "identity.sku" in update_fields:
            sku_update = _safe_text(row.get("identity.sku"))
            if sku_update and len(sku_update) > SKU_MAX_LENGTH:
                issues.append(f"identity.sku exceeds {SKU_MAX_LENGTH} characters.")
        if "salesChannels.productName" in update_fields:
            product_name_update = _safe_text(row.get("salesChannels.productName"))
            if product_name_update and len(product_name_update) > PRODUCT_NAME_MAX_LENGTH:
                issues.append(
                    f"salesChannels.productName exceeds {PRODUCT_NAME_MAX_LENGTH} characters."
                )
        for field in ("identity.ean", "identity.upc", "identity.isbn", "identity.mpn", "identity.barcode"):
            if field in update_fields:
                value = _safe_text(row.get(field))
                if value and len(value) > IDENTITY_MAX_LENGTH:
                    issues.append(f"{field} exceeds max length.")

        if product_id:
            if "salesChannels.categories" in update_fields and category_values:
                cur.execute(
                    "SELECT categoryCode FROM product_catalogue_category WHERE productId = ?",
                    (product_id,),
                )
                existing = {str(value[0]) for value in cur.fetchall() if value[0] is not None}
                duplicates = existing.intersection(category_values)
                if duplicates:
                    log(
                        "⚠️ Category already assigned: "
                        + ", ".join(sorted(duplicates)),
                        log_callback,
                    )
            if "variations" in update_fields and combined_variations:
                cur.execute(
                    """
                    SELECT optionId, optionValueId
                    FROM product_catalogue_options
                    WHERE productId = ?
                    """,
                    (product_id,),
                )
                existing = {(row[0], row[1]) for row in cur.fetchall()}
                duplicates = {pair for pair in combined_variations if pair in existing}
                if duplicates:
                    duplicate_variations += len(duplicates)
                    log(
                        f"⚠️ Duplicate variations already assigned for {sku_value or product_id}.",
                        log_callback,
                    )
            if "seasonIds" in update_fields and season_values:
                cur.execute(
                    "SELECT seasonId FROM product_catalogue_seasons WHERE productId = ?",
                    (product_id,),
                )
                existing = {row[0] for row in cur.fetchall()}
                duplicates = {value for value in season_values if value in existing}
                if duplicates:
                    issues.append("Duplicate seasonIds already assigned.")
            if "composition.bundleComponents" in update_fields and bundle_components:
                cur.execute(
                    """
                    SELECT componentProductId
                    FROM product_catalogue_bundle
                    WHERE productId = ?
                    """,
                    (product_id,),
                )
                existing = {row[0] for row in cur.fetchall()}
                duplicates = {value for value, _qty in bundle_components if value in existing}
                if duplicates:
                    issues.append("Duplicate bundle components already assigned.")

        if row_missing_options:
            for missing in sorted(row_missing_options):
                if missing in option_headers:
                    issues.append(f"option:{missing}")
        for option_name, option_value_name in row_missing_option_values:
            if option_name in option_headers and _safe_text(row.get(option_name)) == option_value_name:
                issues.append(f"option:{option_name}:{option_value_name}")

        if issues:
            row_copy = dict(row)
            row_copy["issues"] = "; ".join(issues)
            invalid_rows.append(row_copy)
            continue

        reporting_category_id_value = None
        if "reporting.categoryId" in update_fields:
            raw_reporting_category = _safe_text(row.get("reporting.categoryId"))
            if raw_reporting_category:
                if raw_reporting_category.isdigit():
                    reporting_category_id_value = int(raw_reporting_category)
                else:
                    cur.execute(
                        "SELECT id FROM ref_product_category WHERE lower(name) = lower(?)",
                        (raw_reporting_category,),
                    )
                    result = cur.fetchone()
                    reporting_category_id_value = result[0] if result else None
                if reporting_category_id_value is None:
                    issues.append(f"Unknown reporting.categoryId {raw_reporting_category}.")

        reporting_subcategory_id_value = None
        if "reporting.subcategoryId" in update_fields:
            raw_reporting_subcategory = _safe_text(row.get("reporting.subcategoryId"))
            if raw_reporting_subcategory:
                if raw_reporting_subcategory.isdigit():
                    reporting_subcategory_id_value = int(raw_reporting_subcategory)
                else:
                    cur.execute(
                        "SELECT id FROM ref_product_category WHERE lower(name) = lower(?)",
                        (raw_reporting_subcategory,),
                    )
                    result = cur.fetchone()
                    reporting_subcategory_id_value = result[0] if result else None
                if reporting_subcategory_id_value is None:
                    issues.append(f"Unknown reporting.subcategoryId {raw_reporting_subcategory}.")

        reporting_season_id_value = None
        if "reporting.seasonId" in update_fields:
            raw_reporting_season = _safe_text(row.get("reporting.seasonId"))
            if raw_reporting_season:
                if raw_reporting_season.isdigit():
                    reporting_season_id_value = int(raw_reporting_season)
                else:
                    cur.execute(
                        "SELECT id FROM ref_season WHERE lower(name) = lower(?)",
                        (raw_reporting_season,),
                    )
                    result = cur.fetchone()
                    reporting_season_id_value = result[0] if result else None
                if reporting_season_id_value is None:
                    issues.append(f"Unknown reporting.seasonId {raw_reporting_season}.")

        if issues:
            row_copy = dict(row)
            row_copy["issues"] = "; ".join(issues)
            invalid_rows.append(row_copy)
            continue

        values = {"productId": product_id, "sku": sku_value}
        if "variations" in update_fields and combined_variations:
            values["variations"] = "|".join(
                f"{option_id}:{option_value_id}"
                for option_id, option_value_id in combined_variations
            )
        if brand_id_value is not None:
            values["brandId"] = str(brand_id_value)
        if product_type_id_value is not None:
            values["productTypeId"] = str(product_type_id_value)
        if category_values:
            values["salesChannels.categories"] = ",".join(category_values)
        if season_values:
            values["seasonIds"] = ",".join(str(value) for value in season_values)
        if reporting_category_id_value is not None:
            values["reporting.categoryId"] = str(reporting_category_id_value)
        if reporting_subcategory_id_value is not None:
            values["reporting.subcategoryId"] = str(reporting_subcategory_id_value)
        if reporting_season_id_value is not None:
            values["reporting.seasonId"] = str(reporting_season_id_value)
        if primary_supplier_id_value is not None:
            values["primarySupplier"] = str(primary_supplier_id_value)
        if supplier_id_values:
            values["Supplier"] = json.dumps(sorted(set(supplier_id_values)))
        for field in update_fields:
            if field == "variations" and "variations" in values:
                continue
            if field == "brandId" and "brandId" in values:
                continue
            if field == "productTypeId" and "productTypeId" in values:
                continue
            if field == "salesChannels.categories" and "salesChannels.categories" in values:
                continue
            if field == "seasonIds" and "seasonIds" in values:
                continue
            if field == "reporting.categoryId" and "reporting.categoryId" in values:
                continue
            if field == "reporting.subcategoryId" and "reporting.subcategoryId" in values:
                continue
            if field == "reporting.seasonId" and "reporting.seasonId" in values:
                continue
            if field == "primarySupplier" and "primarySupplier" in values:
                continue
            if field == "Supplier" and "Supplier" in values:
                continue
            values[field] = row.get(field)
        columns = ", ".join(_quote_identifier(name) for name in values.keys())
        placeholders = ", ".join(["?"] * len(values))
        cur.execute(
            f"INSERT INTO validated_product_update ({columns}) VALUES ({placeholders})",
            list(values.values()),
        )
        inserted += 1

    conn.commit()
    _store_update_config(conn, identifier, update_fields)
    conn.close()

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)
    if invalid_rows and unmatched_dir:
        filename = os.path.join(
            unmatched_dir,
            f"{account_name}_product_update_unmatched.csv",
        )
        fieldnames = list(invalid_rows[0].keys())
        with open(filename, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(invalid_rows)
        log(f"⚠️ {len(invalid_rows)} update rows written to {filename}", log_callback)
    return {
        "inserted": inserted,
        "invalid_rows": len(invalid_rows),
        "duplicate_variations": duplicate_variations,
    }


def _fetch_existing_sales_channel(conn: sqlite3.Connection, product_id: int) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT salesChannelName, productName, productCondition, description_text, shortDescription_text
        FROM product_catalogue_core
        WHERE productId = ?
        """,
        (product_id,),
    )
    return cur.fetchone()


def _fetch_existing_categories(conn: sqlite3.Connection, product_id: int) -> List[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT categoryCode FROM product_catalogue_category WHERE productId = ?",
        (product_id,),
    )
    return [str(row[0]) for row in cur.fetchall() if row[0] is not None]


def _fetch_existing_variations(conn: sqlite3.Connection, product_id: int) -> List[Tuple[int, int]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT optionId, optionValueId
        FROM product_catalogue_options
        WHERE productId = ?
        """,
        (product_id,),
    )
    return [(row[0], row[1]) for row in cur.fetchall() if row[0] is not None and row[1] is not None]


def _fetch_existing_seasons(conn: sqlite3.Connection, product_id: int) -> List[int]:
    cur = conn.cursor()
    cur.execute(
        "SELECT seasonId FROM product_catalogue_seasons WHERE productId = ?",
        (product_id,),
    )
    return [row[0] for row in cur.fetchall() if row[0] is not None]


def _fetch_existing_bundle_components(
    conn: sqlite3.Connection, product_id: int
) -> List[Tuple[int, float]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT componentProductId, componentQty
        FROM product_catalogue_bundle
        WHERE productId = ?
        """,
        (product_id,),
    )
    return [
        (row[0], row[1])
        for row in cur.fetchall()
        if row[0] is not None and row[1] is not None
    ]


def _fetch_existing_bundle_flag(conn: sqlite3.Connection, product_id: int) -> Optional[bool]:
    cur = conn.cursor()
    cur.execute("SELECT bundleFlag FROM product_catalogue_core WHERE productId = ?", (product_id,))
    row = cur.fetchone()
    if not row:
        return None
    if row[0] is None:
        return None
    return bool(row[0])


def _merge_unique_ordered(items: List[object], additions: List[object]) -> List[object]:
    seen = set()
    merged: List[object] = []
    for item in items + additions:
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


def sync_product_updates(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_product_update_tables(conn)
    cur = conn.cursor()
    identifier, update_fields = _load_update_config(conn)
    if not identifier:
        log("⚠️ No product update configuration found. Run validation first.", log_callback)
        conn.close()
        return 0

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='validated_product_update'")
    if not cur.fetchone():
        log("⚠️ No validated product update table found. Run validation first.", log_callback)
        conn.close()
        return 0

    ensure_processing_columns(conn, "validated_product_update")
    cur.execute(f"SELECT * FROM validated_product_update WHERE {unprocessed_where_clause()}")
    rows = cur.fetchall()
    total = len(rows)
    if total == 0:
        log("⚠️ No validated updates to sync.", log_callback)
        conn.close()
        return 0

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
    headers = {**credentials.headers, "Content-Type": "application/json"}

    updated = 0
    row_base_success: Dict[int, bool] = {}
    pending_primary_supplier_by_product: Dict[int, int] = {}
    for idx, row in enumerate(rows, 1):
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Update sync cancelled at {idx - 1}/{total}.", log_callback)
            break

        product_id = _safe_int(_row_value(row, "productId"))
        if product_id is None:
            continue

        payload: Dict[str, object] = {}

        for field in ("brandId", "productTypeId", "collectionId", "productGroupId"):
            if field in update_fields:
                value = _safe_int(_row_value(row, field))
                if value is not None:
                    payload[field] = value

        identity = {}
        for field in ("identity.sku", "identity.ean", "identity.upc", "identity.isbn", "identity.mpn", "identity.barcode"):
            if field in update_fields:
                value = _safe_text(_row_value(row, field))
                if value:
                    identity[field.split(".", 1)[1]] = value
        if identity:
            payload["identity"] = identity

        stock = {}
        if "stock.stockTracked" in update_fields:
            value = _safe_bool(_row_value(row, "stock.stockTracked"))
            if value is not None:
                stock["stockTracked"] = value
        if "stock.weight" in update_fields:
            value = _safe_float(_row_value(row, "stock.weight"))
            if value is not None:
                stock["weight"] = {"magnitude": value}

        dimensions = {}
        for field in ("dimensions.width", "dimensions.length", "dimensions.height"):
            if field in update_fields:
                value = _safe_float(_row_value(row, field))
                if value is not None:
                    dimensions[field.split(".", 1)[1]] = value
        if dimensions:
            stock["dimensions"] = dimensions
        if stock:
            payload["stock"] = stock

        financial = {}
        if "financialDetails.taxable" in update_fields:
            value = _safe_bool(_row_value(row, "financialDetails.taxable"))
            if value is not None:
                financial["taxable"] = value
        if "financialDetails.taxCode" in update_fields:
            tax_code = _safe_text(_row_value(row, "financialDetails.taxCode"))
            if tax_code:
                if str(tax_code).isdigit():
                    financial["taxCode"] = {"id": int(tax_code)}
                else:
                    financial["taxCode"] = {"code": tax_code}
        if financial:
            payload["financialDetails"] = financial

        if any(field.startswith("salesChannels.") for field in update_fields):
            channel = {}
            existing_channel = _fetch_existing_sales_channel(conn, product_id)
            channel["salesChannelName"] = (
                existing_channel[0] if existing_channel and existing_channel[0] else "Brightpearl"
            )

            if "salesChannels.productName" in update_fields:
                value = _safe_text(_row_value(row, "salesChannels.productName"))
                if value:
                    channel["productName"] = value
            elif existing_channel and existing_channel[1]:
                channel["productName"] = existing_channel[1]

            if "salesChannels.productCondition" in update_fields:
                value = _safe_text(_row_value(row, "salesChannels.productCondition"))
                if value:
                    channel["productCondition"] = value
            elif existing_channel and existing_channel[2]:
                channel["productCondition"] = existing_channel[2]

            if "salesChannels.description.text" in update_fields:
                value = _safe_text(_row_value(row, "salesChannels.description.text"))
                if value is not None:
                    channel["description"] = {
                        "languageCode": "en",
                        "text": value,
                        "format": "HTML_FRAGMENT",
                    }
            elif existing_channel and existing_channel[3]:
                channel["description"] = {
                    "languageCode": "en",
                    "text": existing_channel[3],
                    "format": "HTML_FRAGMENT",
                }

            if "salesChannels.shortDescription.text" in update_fields:
                value = _safe_text(_row_value(row, "salesChannels.shortDescription.text"))
                if value is not None:
                    channel["shortDescription"] = {
                        "languageCode": "en",
                        "text": value,
                        "format": "HTML_FRAGMENT",
                    }
            elif existing_channel and existing_channel[4]:
                channel["shortDescription"] = {
                    "languageCode": "en",
                    "text": existing_channel[4],
                    "format": "HTML_FRAGMENT",
                }

            categories = []
            if "salesChannels.categories" in update_fields:
                categories = _split_list(_row_value(row, "salesChannels.categories"))
            existing_categories = _fetch_existing_categories(conn, product_id)
            if existing_categories or categories:
                merged = _merge_unique_ordered(existing_categories, categories)
                channel["categories"] = [{"categoryCode": str(value)} for value in merged]

            payload["salesChannels"] = [channel]

        existing_variations = _fetch_existing_variations(conn, product_id)
        new_variations = (
            _parse_variations(_row_value(row, "variations"))
            if "variations" in update_fields
            else []
        )
        merged_variations = _merge_unique_ordered(existing_variations, new_variations)
        if merged_variations:
            payload["variations"] = [
                {"optionId": option_id, "optionValueId": option_value_id}
                for option_id, option_value_id in merged_variations
            ]

        if "seasonIds" in update_fields:
            existing_seasons = _fetch_existing_seasons(conn, product_id)
            new_seasons = [
                _safe_int(value)
                for value in _split_list(_row_value(row, "seasonIds"))
                if value
            ]
            merged = _merge_unique_ordered(existing_seasons, [value for value in new_seasons if value])
            payload["seasonIds"] = merged

        if "nominalCodeStock" in update_fields:
            value = _safe_text(_row_value(row, "nominalCodeStock"))
            if value:
                payload["nominalCodeStock"] = value
        if "nominalCodePurchases" in update_fields:
            value = _safe_text(_row_value(row, "nominalCodePurchases"))
            if value:
                payload["nominalCodePurchases"] = value
        if "nominalCodeSales" in update_fields:
            value = _safe_text(_row_value(row, "nominalCodeSales"))
            if value:
                payload["nominalCodeSales"] = value

        reporting = {}
        if "reporting.categoryId" in update_fields:
            value = _safe_int(_row_value(row, "reporting.categoryId"))
            if value is not None:
                reporting["categoryId"] = value
        if "reporting.subcategoryId" in update_fields:
            value = _safe_int(_row_value(row, "reporting.subcategoryId"))
            if value is not None:
                reporting["subcategoryId"] = value
        if "reporting.seasonId" in update_fields:
            value = _safe_int(_row_value(row, "reporting.seasonId"))
            if value is not None:
                reporting["seasonId"] = value
        if reporting:
            payload["reporting"] = reporting

        if "composition.bundle" in update_fields or "composition.bundleComponents" in update_fields:
            bundle_value = (
                _safe_bool(_row_value(row, "composition.bundle"))
                if "composition.bundle" in update_fields
                else None
            )
            existing_components = _fetch_existing_bundle_components(conn, product_id)
            new_components = _parse_components_by_id(
                _row_value(row, "composition.bundleComponents")
            )
            if bundle_value is False:
                payload["composition"] = {"bundle": False, "bundleComponents": []}
            else:
                merged_components = _merge_unique_ordered(existing_components, new_components)
                bundle_flag = bundle_value
                if bundle_flag is None:
                    bundle_flag = _fetch_existing_bundle_flag(conn, product_id) or bool(merged_components)
                payload["composition"] = {
                    "bundle": bool(bundle_flag),
                    "bundleComponents": [
                        {"productId": component_id, "productQuantity": qty}
                        for component_id, qty in merged_components
                    ],
                }

        ok = True
        requests_remaining = 0
        throttle_ms = 0
        if payload:
            url = f"{base}/product-service/product/{product_id}"
            ok, requests_remaining, throttle_ms = _put_json(
                url,
                headers,
                payload,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )

        supplier_updates_ok = True
        primary_supplier_id = None
        if "primarySupplier" in update_fields:
            primary_supplier_id = _safe_int(_row_value(row, "primarySupplier"))

        if ok and "Supplier" in update_fields:
            supplier_ids = _parse_int_list_json(_row_value(row, "Supplier"))
            if supplier_ids:
                supplier_url = f"{base}/product-service/product/{product_id}/supplier"
                supplier_payload = sorted(set(supplier_ids))
                supplier_ok, _, supplier_remaining, supplier_throttle = _post_json(
                    supplier_url,
                    headers,
                    supplier_payload,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
                supplier_updates_ok = supplier_updates_ok and supplier_ok
                if should_pause_for_throttle(supplier_remaining, supplier_throttle):
                    sleep_with_cancel_ms(supplier_throttle, cancel_token, log_callback)

        base_success = ok and supplier_updates_ok
        row_base_success[product_id] = base_success
        if base_success and primary_supplier_id is not None:
            pending_primary_supplier_by_product[product_id] = primary_supplier_id
            log(f"✅ Updated product {product_id} (queued primarySupplier assignment).", log_callback)
        elif base_success:
            mark_rows_processed(conn, "validated_product_update", row["id"], f"Updated product {product_id}")
            conn.commit()
            updated += 1
            log(f"✅ Updated product {product_id}.", log_callback)
        else:
            log(f"❌ Failed to update product {product_id}.", log_callback)

        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

        progress = int((idx / max(total, 1)) * 100)
        log(f"PROGRESS:{progress}", log_callback)

    primary_success_products: set[int] = set()
    if pending_primary_supplier_by_product:
        products_by_primary_supplier: Dict[int, List[int]] = {}
        for product_id, primary_supplier_id in pending_primary_supplier_by_product.items():
            products_by_primary_supplier.setdefault(primary_supplier_id, []).append(product_id)

        for primary_supplier_id, product_ids in products_by_primary_supplier.items():
            if cancel_token and cancel_token.is_set():
                break
            unique_product_ids = sorted(set(product_ids))
            product_id_chunks = _chunk_list(unique_product_ids, 500)
            url = f"{base}/product-service/primary-supplier/{primary_supplier_id}/product"
            for chunk_index, product_id_chunk in enumerate(product_id_chunks, 1):
                if cancel_token and cancel_token.is_set():
                    break
                payload = {"productIds": product_id_chunk}
                primary_ok, primary_remaining, primary_throttle = _put_json(
                    url,
                    headers,
                    payload,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
                if primary_ok:
                    primary_success_products.update(product_id_chunk)
                    log(
                        "✅ Applied primarySupplier "
                        f"{primary_supplier_id} to {len(product_id_chunk)} product(s) "
                        f"(batch {chunk_index}/{len(product_id_chunks)}).",
                        log_callback,
                    )
                else:
                    log(
                        "❌ Failed primarySupplier "
                        f"{primary_supplier_id} for batch {chunk_index}/{len(product_id_chunks)}.",
                        log_callback,
                    )
                if should_pause_for_throttle(primary_remaining, primary_throttle):
                    sleep_with_cancel_ms(primary_throttle, cancel_token, log_callback)

        for product_id, primary_supplier_id in pending_primary_supplier_by_product.items():
            if product_id in primary_success_products:
                row_id = next((r["id"] for r in rows if _safe_int(_row_value(r, "productId")) == product_id), None)
                if row_id is not None:
                    mark_rows_processed(conn, "validated_product_update", row_id, f"Updated product {product_id} and primary supplier")
                    conn.commit()
                updated += 1
                log(
                    f"✅ primarySupplier assignment confirmed for product {product_id} ({primary_supplier_id}).",
                    log_callback,
                )
            else:
                row_base_success[product_id] = False
                log(
                    f"❌ primarySupplier assignment failed for product {product_id} ({primary_supplier_id}).",
                    log_callback,
                )

    conn.close()
    return updated
