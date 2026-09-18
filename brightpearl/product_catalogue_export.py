"""Product catalogue sync/export helpers for the Export > Product Catalogue module."""
from __future__ import annotations

import csv
from csv_safety import open_table
import json
import sqlite3
from typing import Dict, Iterable, List, Optional

import requests

from .common import (
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    log,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)
from .performance import record_api_update
from .product_import import sync_product_reference_data


PRODUCT_EXPORT_TABLES: Iterable[str] = (
    "export_products",
    "export_product_suppliers",
    "export_product_sales_channels",
    "export_product_categories",
    "export_product_variations",
    "export_product_seasons",
    "export_product_bundle_components",
    "export_product_custom_fields",
    "export_product_warehouses",
)


def _ensure_export_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_products (
            productId INTEGER PRIMARY KEY,
            brandId INTEGER,
            collectionId INTEGER,
            productTypeId INTEGER,
            productGroupId INTEGER,
            nominalCodeStock TEXT,
            nominalCodePurchases TEXT,
            nominalCodeSales TEXT,
            sku TEXT,
            barcode TEXT,
            ean TEXT,
            upc TEXT,
            isbn TEXT,
            mpn TEXT,
            featured INTEGER,
            stockTracked INTEGER,
            weightMagnitude REAL,
            dimensionsLength REAL,
            dimensionsHeight REAL,
            dimensionsWidth REAL,
            dimensionsVolume REAL,
            taxable INTEGER,
            taxCodeId INTEGER,
            taxCodeCode TEXT,
            bundle INTEGER,
            createdOn TEXT,
            updatedOn TEXT,
            reportingCategoryId INTEGER,
            reportingSubcategoryId INTEGER,
            reportingSeasonId INTEGER,
            primarySupplierId INTEGER,
            status TEXT,
            salesPopupMessage TEXT,
            version INTEGER,
            raw_json TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_suppliers (
            productId INTEGER,
            supplierContactId INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_sales_channels (
            productId INTEGER,
            salesChannelName TEXT,
            productName TEXT,
            productCondition TEXT,
            descriptionLanguageCode TEXT,
            descriptionText TEXT,
            shortDescriptionLanguageCode TEXT,
            shortDescriptionText TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_categories (
            productId INTEGER,
            salesChannelName TEXT,
            categoryCode TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_variations (
            productId INTEGER,
            optionId INTEGER,
            optionValueId INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_seasons (
            productId INTEGER,
            seasonId INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_bundle_components (
            productId INTEGER,
            componentProductId INTEGER,
            componentQty REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_custom_fields (
            productId INTEGER,
            fieldCode TEXT,
            fieldId INTEGER,
            valueText TEXT,
            valueJson TEXT,
            is_null INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute("PRAGMA table_info(export_product_custom_fields)")
    custom_field_columns = {row[1] for row in cur.fetchall()}
    if "isNull" in custom_field_columns and "is_null" not in custom_field_columns:
        cur.execute(
            "ALTER TABLE export_product_custom_fields RENAME COLUMN isNull TO is_null"
        )
    cur.execute("PRAGMA table_info(export_products)")
    product_columns = {row[1] for row in cur.fetchall()}
    product_column_migrations = {
        "primarySupplierId": "INTEGER",
        # Older databases predate the complete identity block. Keep this
        # migration here so users do not need to delete and re-sync their DB.
        "ean": "TEXT",
        "upc": "TEXT",
        "isbn": "TEXT",
        "mpn": "TEXT",
    }
    for column_name, column_type in product_column_migrations.items():
        if column_name not in product_columns:
            cur.execute(
                f"ALTER TABLE export_products ADD COLUMN {column_name} {column_type}"
            )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS export_product_warehouses (
            productId INTEGER,
            warehouseId INTEGER,
            defaultLocationId INTEGER,
            reorderLevel REAL,
            reorderQuantity REAL
        )
        """
    )
    conn.commit()


def _clear_export_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for table in PRODUCT_EXPORT_TABLES:
        cur.execute(f"DELETE FROM {table}")
    conn.commit()


def _append_query(url: str, query: str) -> str:
    return f"{url}&{query}" if "?" in url else f"{url}?{query}"


def _fetch_product_uris(option_url: str, headers: dict, *, log_callback=None, cancel_token=None) -> List[str]:
    if cancel_token and cancel_token.is_set():
        return []
    try:
        response = requests.options(option_url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        payload = response.json().get("response", {})
        uris = payload.get("getUris", [])
        if isinstance(uris, list):
            return [uri for uri in uris if isinstance(uri, str)]
    except requests.RequestException as exc:
        log(f"❌ OPTIONS request failed for product export sync: {exc}", log_callback)
    except ValueError as exc:
        log(f"❌ Failed to parse OPTIONS response for product export sync: {exc}", log_callback)
    return []


def _extract_custom_field_value(value: object) -> tuple[Optional[int], Optional[str], str]:
    if isinstance(value, dict):
        value_json = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return value.get("id"), value.get("value"), value_json
    if value is None:
        return None, None, "null"
    value_json = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return None, str(value), value_json


def _load_reference_map(
    conn: sqlite3.Connection,
    table: str,
    key_col: str,
    value_col: str,
) -> Dict[int, str]:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {key_col}, {value_col} FROM {table}")
    except sqlite3.Error:
        return {}
    lookup: Dict[int, str] = {}
    for row in cur.fetchall():
        try:
            row_id = int(row[0])
        except (TypeError, ValueError):
            continue
        row_name = str(row[1]).strip() if row[1] is not None else ""
        if row_name:
            lookup[row_id] = row_name
    return lookup


def _resolve_reference_name(lookup: Dict[int, str], value: object) -> str:
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else ""
    return lookup.get(int_value, str(value) if value is not None else "")


def sync_product_catalogue_export_data(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Sync full product payloads (including custom fields) for export."""
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    log("➡️ Syncing product reference data for export...", log_callback)
    sync_product_reference_data(
        account_name,
        db_path,
        log_callback=log_callback,
        cancel_token=cancel_token,
    )
    if cancel_token and cancel_token.is_set():
        return 0

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/product-service"
    headers = {**credentials.headers, "Content-Type": "application/json"}

    uris = _fetch_product_uris(f"{base}/product", headers, log_callback=log_callback, cancel_token=cancel_token)
    if not uris:
        uris = ["/product"]

    conn = sqlite3.connect(db_path)
    _ensure_export_tables(conn)
    _clear_export_tables(conn)
    cur = conn.cursor()

    synced = 0
    total_uris = max(len(uris), 1)
    for idx, uri in enumerate(uris, 1):
        if cancel_token and cancel_token.is_set():
            break

        url = _append_query(f"{base}{uri}", "includeOptional=customFields,nullCustomFields")
        json_response, throttle_ms, requests_remaining = send_request(
            url,
            headers,
            cancel_token=cancel_token,
            log_callback=log_callback,
        )

        if cancel_token and cancel_token.is_set():
            break
        if not json_response:
            continue

        try:
            payload = json.loads(json_response).get("response", [])
        except json.JSONDecodeError as exc:
            log(f"⚠️ Failed to parse product export payload: {exc}", log_callback)
            continue
        if not isinstance(payload, list):
            continue

        for product in payload:
            product_id = product.get("id")
            if product_id is None:
                continue

            stock = product.get("stock", {}) or {}
            dimensions = stock.get("dimensions", {}) or {}
            financial = product.get("financialDetails", {}) or {}
            tax_code = financial.get("taxCode", {}) or {}
            reporting = product.get("reporting", {}) or {}
            composition = product.get("composition", {}) or {}
            identity = product.get("identity", {}) or {}

            cur.execute(
                """
                INSERT OR REPLACE INTO export_products (
                    productId,
                    brandId,
                    collectionId,
                    productTypeId,
                    productGroupId,
                    nominalCodeStock,
                    nominalCodePurchases,
                    nominalCodeSales,
                    sku,
                    barcode,
                    ean,
                    upc,
                    isbn,
                    mpn,
                    featured,
                    stockTracked,
                    weightMagnitude,
                    dimensionsLength,
                    dimensionsHeight,
                    dimensionsWidth,
                    dimensionsVolume,
                    taxable,
                    taxCodeId,
                    taxCodeCode,
                    bundle,
                    createdOn,
                    updatedOn,
                    reportingCategoryId,
                    reportingSubcategoryId,
                    reportingSeasonId,
                    primarySupplierId,
                    status,
                    salesPopupMessage,
                    version,
                    raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    product_id,
                    product.get("brandId"),
                    product.get("collectionId"),
                    product.get("productTypeId"),
                    product.get("productGroupId"),
                    product.get("nominalCodeStock"),
                    product.get("nominalCodePurchases"),
                    product.get("nominalCodeSales"),
                    identity.get("sku"),
                    identity.get("barcode"),
                    identity.get("ean"),
                    identity.get("upc"),
                    identity.get("isbn"),
                    identity.get("mpn"),
                    int(bool(product.get("featured"))),
                    int(bool(stock.get("stockTracked"))),
                    (stock.get("weight", {}) or {}).get("magnitude"),
                    dimensions.get("length"),
                    dimensions.get("height"),
                    dimensions.get("width"),
                    dimensions.get("volume"),
                    int(bool(financial.get("taxable"))),
                    tax_code.get("id"),
                    tax_code.get("code"),
                    int(bool(composition.get("bundle"))),
                    product.get("createdOn"),
                    product.get("updatedOn"),
                    reporting.get("categoryId"),
                    reporting.get("subcategoryId"),
                    reporting.get("seasonId"),
                    product.get("primarySupplierId"),
                    product.get("status"),
                    product.get("salesPopupMessage"),
                    product.get("version"),
                    json.dumps(product, separators=(",", ":"), sort_keys=True),
                ),
            )

            for channel in product.get("salesChannels", []) or []:
                if not isinstance(channel, dict):
                    continue
                desc = channel.get("description", {}) or {}
                short_desc = channel.get("shortDescription", {}) or {}
                channel_name = channel.get("salesChannelName")

                cur.execute(
                    """
                    INSERT INTO export_product_sales_channels (
                        productId,
                        salesChannelName,
                        productName,
                        productCondition,
                        descriptionLanguageCode,
                        descriptionText,
                        shortDescriptionLanguageCode,
                        shortDescriptionText
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        product_id,
                        channel_name,
                        channel.get("productName"),
                        channel.get("productCondition"),
                        desc.get("languageCode") if isinstance(desc, dict) else None,
                        desc.get("text") if isinstance(desc, dict) else None,
                        short_desc.get("languageCode") if isinstance(short_desc, dict) else None,
                        short_desc.get("text") if isinstance(short_desc, dict) else None,
                    ),
                )

                for category in channel.get("categories", []) or []:
                    if not isinstance(category, dict):
                        continue
                    category_code = category.get("categoryCode")
                    if category_code is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO export_product_categories (
                            productId,
                            salesChannelName,
                            categoryCode
                        ) VALUES (?,?,?)
                        """,
                        (product_id, channel_name, str(category_code)),
                    )

            for variation in product.get("variations", []) or []:
                if not isinstance(variation, dict):
                    continue
                cur.execute(
                    """
                    INSERT INTO export_product_variations (productId, optionId, optionValueId)
                    VALUES (?,?,?)
                    """,
                    (
                        product_id,
                        variation.get("optionId"),
                        variation.get("optionValueId"),
                    ),
                )

            for season_id in product.get("seasonIds", []) or []:
                cur.execute(
                    "INSERT INTO export_product_seasons (productId, seasonId) VALUES (?,?)",
                    (product_id, season_id),
                )

            for component in composition.get("bundleComponents", []) or []:
                if not isinstance(component, dict):
                    continue
                cur.execute(
                    """
                    INSERT INTO export_product_bundle_components (productId, componentProductId, componentQty)
                    VALUES (?,?,?)
                    """,
                    (product_id, component.get("productId"), component.get("productQuantity")),
                )

            custom_fields = product.get("customFields", {}) or {}
            if isinstance(custom_fields, dict):
                for field_code, field_value in custom_fields.items():
                    field_id, value_text, value_json = _extract_custom_field_value(field_value)
                    cur.execute(
                        """
                        INSERT INTO export_product_custom_fields (
                            productId,
                            fieldCode,
                            fieldId,
                            valueText,
                            valueJson,
                            is_null
                        ) VALUES (?,?,?,?,?,0)
                        """,
                        (product_id, field_code, field_id, value_text, value_json),
                    )

            for field_code in product.get("nullCustomFields", []) or []:
                cur.execute(
                    """
                    INSERT INTO export_product_custom_fields (
                        productId,
                        fieldCode,
                        fieldId,
                        valueText,
                        valueJson,
                            is_null
                    ) VALUES (?,?,?,?,?,1)
                    """,
                    (product_id, field_code, None, None, "null"),
                )

            warehouses = product.get("warehouses", {}) or {}
            if isinstance(warehouses, dict):
                for warehouse_id, warehouse_data in warehouses.items():
                    if not isinstance(warehouse_data, dict):
                        continue
                    try:
                        warehouse_id_value = int(warehouse_id)
                    except (TypeError, ValueError):
                        warehouse_id_value = None
                    cur.execute(
                        """
                        INSERT INTO export_product_warehouses (
                            productId,
                            warehouseId,
                            defaultLocationId,
                            reorderLevel,
                            reorderQuantity
                        ) VALUES (?,?,?,?,?)
                        """,
                        (
                            product_id,
                            warehouse_id_value,
                            warehouse_data.get("defaultLocationId"),
                            warehouse_data.get("reorderLevel"),
                            warehouse_data.get("reorderQuantity"),
                        ),
                    )

            synced += 1

        conn.commit()
        record_api_update(len(payload))

        if should_pause_for_throttle(requests_remaining, throttle_ms):
            log(f"⏳ Throttling: waiting {throttle_ms} ms...", log_callback)
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

        progress = int((idx / total_uris) * 100)
        log(f"PROGRESS:{progress}", log_callback)

    conn.close()
    return synced


def sync_export_product_suppliers(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Sync product supplier contact IDs for already-synced export products."""
    ensure_account_binding(db_path, account_name)
    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    conn = sqlite3.connect(db_path)
    _ensure_export_tables(conn)
    cur = conn.cursor()
    cur.execute("SELECT productId FROM export_products ORDER BY productId")
    product_ids = [int(row[0]) for row in cur.fetchall() if row[0] is not None]
    cur.execute("DELETE FROM export_product_suppliers")
    conn.commit()

    if not product_ids:
        conn.close()
        return 0

    base = f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/product-service"
    batch_size = 200
    inserted = 0
    total_batches = max((len(product_ids) + batch_size - 1) // batch_size, 1)

    for batch_index in range(total_batches):
        if cancel_token and cancel_token.is_set():
            break
        batch_ids = product_ids[batch_index * batch_size : (batch_index + 1) * batch_size]
        id_set = ",".join(str(product_id) for product_id in batch_ids)
        url = f"{base}/product/{id_set}/supplier"
        json_response, throttle_ms, requests_remaining = send_request(
            url,
            credentials.headers,
            cancel_token=cancel_token,
            log_callback=log_callback,
        )
        if not json_response:
            continue

        try:
            payload = json.loads(json_response).get("response", {}) or {}
        except json.JSONDecodeError as exc:
            log(f"⚠️ Failed to parse supplier payload: {exc}", log_callback)
            continue
        if not isinstance(payload, dict):
            continue

        for product_id_key, supplier_contact_ids in payload.items():
            try:
                product_id = int(product_id_key)
            except (TypeError, ValueError):
                continue
            if not isinstance(supplier_contact_ids, list):
                continue
            for supplier_contact_id in supplier_contact_ids:
                try:
                    contact_id = int(supplier_contact_id)
                except (TypeError, ValueError):
                    continue
                cur.execute(
                    """
                    INSERT INTO export_product_suppliers (productId, supplierContactId)
                    VALUES (?,?)
                    """,
                    (product_id, contact_id),
                )
                inserted += 1

        conn.commit()
        record_api_update(len(payload))
        if should_pause_for_throttle(requests_remaining, throttle_ms):
            sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    conn.close()
    return inserted


def export_synced_product_catalogue_to_csv(
    db_path: str,
    file_path: str,
    *,
    include_price_lists: bool = False,
    include_suppliers: bool = False,
) -> int:
    """Export synced full catalogue data to a CSV file."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_export_tables(conn)
    cur = conn.cursor()

    cur.execute("SELECT * FROM export_products ORDER BY productId")
    products = cur.fetchall()
    if not products:
        conn.close()
        return 0

    cur.execute("SELECT DISTINCT fieldCode FROM export_product_custom_fields ORDER BY fieldCode")
    custom_field_codes = [r[0] for r in cur.fetchall() if r[0]]

    brand_lookup = _load_reference_map(conn, "ref_brands", "brandId", "brandName")
    product_type_lookup = _load_reference_map(conn, "ref_type", "id", "name")
    category_lookup = _load_reference_map(conn, "ref_product_category", "id", "name")
    season_lookup = _load_reference_map(conn, "ref_season", "id", "name")
    option_lookup = _load_reference_map(conn, "ref_option", "id", "name")
    warehouse_lookup = _load_reference_map(conn, "ref_warehouses", "warehouseId", "name")
    supplier_lookup = _load_reference_map(conn, "contact_catalogue", "contactId", "companyName")
    # best-effort: table names can vary by account/version.
    collection_lookup = _load_reference_map(conn, "ref_product_collection", "id", "name")
    if not collection_lookup:
        collection_lookup = _load_reference_map(conn, "ref_collection", "id", "name")
    product_group_lookup = _load_reference_map(conn, "ref_product_group", "id", "name")
    if not product_group_lookup:
        product_group_lookup = _load_reference_map(conn, "ref_group", "id", "name")

    price_list_headers: List[str] = []
    price_list_key_to_header: Dict[tuple[int, str], str] = {}
    if include_price_lists:
        cur.execute(
            """
            SELECT DISTINCT
                v.priceListId,
                COALESCE(v.currencyCode, '') AS currencyCode,
                COALESCE(p.name, p.code, CAST(v.priceListId AS TEXT)) AS priceListName
            FROM ref_price_list_values v
            LEFT JOIN ref_price_lists p
              ON p.priceListId = v.priceListId
            ORDER BY priceListName, currencyCode, v.priceListId
            """
        )
        for price_list_id, currency_code, price_list_name in cur.fetchall():
            header = (
                f"{price_list_name} ({currency_code})"
                if currency_code
                else str(price_list_name)
            )
            if header in price_list_headers:
                header = f"{header} [{price_list_id}]"
            price_list_headers.append(header)
            price_list_key_to_header[(int(price_list_id), str(currency_code))] = header

    headers = [
        "productId",
        "sku",
        "productName",
        "barcode",
        "ean",
        "upc",
        "isbn",
        "mpn",
        "brandId",
        "categoryCode",
        "categoryName",
        "productTypeId",
        "collectionId",
        "nominalCodeStock",
        "nominalCodePurchases",
        "nominalCodeSales",
        "featured",
        "stockTracked",
        "weightMagnitude",
        "dimensionsLength",
        "dimensionsHeight",
        "dimensionsWidth",
        "dimensionsVolume",
        "taxable",
        "taxCodeId",
        "taxCodeCode",
        "bundle",
        "reportingCategoryId",
        "reportingSubcategoryId",
        "reportingSeasonId",
        "primarySupplierId",
        "status",
        "salesPopupMessage",
        "productGroupId",
        "seasonIds",
        "salesChannelName",
        "productCondition",
        "descriptionLanguageCode",
        "descriptionText",
        "shortDescriptionLanguageCode",
        "shortDescriptionText",
        "variations",
        "bundleComponents",
        "warehouses",
        "createdOn",
        "updatedOn",
        "version",
    ] + [f"customFields.{code}" for code in custom_field_codes] + price_list_headers
    if include_suppliers:
        headers.append("suppliers")

    # Preload all related rows once instead of querying per product.
    seasons_by_product: Dict[int, List[object]] = {}
    cur.execute("SELECT productId, seasonId FROM export_product_seasons ORDER BY productId, seasonId")
    for product_id, season_id in cur.fetchall():
        if product_id is None or season_id is None:
            continue
        seasons_by_product.setdefault(int(product_id), []).append(season_id)

    sales_channels_by_product: Dict[int, List[sqlite3.Row]] = {}
    cur.execute(
        """
        SELECT productId, salesChannelName, productName, productCondition,
               descriptionLanguageCode, descriptionText,
               shortDescriptionLanguageCode, shortDescriptionText
        FROM export_product_sales_channels
        ORDER BY productId, salesChannelName
        """
    )
    for channel in cur.fetchall():
        product_id = channel["productId"]
        if product_id is None:
            continue
        sales_channels_by_product.setdefault(int(product_id), []).append(channel)

    categories_by_product: Dict[int, List[sqlite3.Row]] = {}
    cur.execute(
        """
        SELECT productId, salesChannelName, categoryCode
        FROM export_product_categories
        ORDER BY productId
        """
    )
    for category in cur.fetchall():
        product_id = category["productId"]
        if product_id is None:
            continue
        categories_by_product.setdefault(int(product_id), []).append(category)

    option_value_lookup: Dict[tuple[int, int], str] = {}
    try:
        cur.execute("SELECT optionId, optionValueId, optionValueName FROM ref_option_values")
        for option_id, option_value_id, option_value_name in cur.fetchall():
            if option_id is None or option_value_id is None or option_value_name is None:
                continue
            option_value_lookup[(int(option_id), int(option_value_id))] = str(option_value_name)
    except sqlite3.Error:
        option_value_lookup = {}

    variations_by_product: Dict[int, List[sqlite3.Row]] = {}
    cur.execute(
        """
        SELECT productId, optionId, optionValueId
        FROM export_product_variations
        ORDER BY productId
        """
    )
    for variation in cur.fetchall():
        product_id = variation["productId"]
        if product_id is None:
            continue
        variations_by_product.setdefault(int(product_id), []).append(variation)

    component_name_lookup: Dict[int, str] = {}
    cur.execute(
        """
        SELECT
            p.productId,
            COALESCE(
                MAX(CASE WHEN s.salesChannelName = 'Brightpearl' THEN s.productName END),
                p.sku,
                CAST(p.productId AS TEXT)
            ) AS componentName
        FROM export_products p
        LEFT JOIN export_product_sales_channels s
          ON s.productId = p.productId
        GROUP BY p.productId, p.sku
        """
    )
    for product_id, component_name in cur.fetchall():
        if product_id is None:
            continue
        component_name_lookup[int(product_id)] = str(component_name) if component_name is not None else str(product_id)

    bundle_components_by_product: Dict[int, List[sqlite3.Row]] = {}
    cur.execute(
        """
        SELECT productId, componentProductId, componentQty
        FROM export_product_bundle_components
        ORDER BY productId
        """
    )
    for component in cur.fetchall():
        product_id = component["productId"]
        if product_id is None:
            continue
        bundle_components_by_product.setdefault(int(product_id), []).append(component)

    warehouses_by_product: Dict[int, List[sqlite3.Row]] = {}
    cur.execute(
        """
        SELECT productId, warehouseId, defaultLocationId, reorderLevel, reorderQuantity
        FROM export_product_warehouses
        ORDER BY productId, warehouseId
        """
    )
    for warehouse in cur.fetchall():
        product_id = warehouse["productId"]
        if product_id is None:
            continue
        warehouses_by_product.setdefault(int(product_id), []).append(warehouse)

    custom_fields_by_product: Dict[int, Dict[str, str]] = {}
    cur.execute(
        """
        SELECT productId, fieldCode, valueText, valueJson, is_null
        FROM export_product_custom_fields
        ORDER BY productId
        """
    )
    for custom_field in cur.fetchall():
        product_id = custom_field["productId"]
        field_code = custom_field["fieldCode"]
        if product_id is None or not field_code:
            continue
        product_custom_fields = custom_fields_by_product.setdefault(int(product_id), {})
        product_custom_fields[str(field_code)] = (
            "" if custom_field["is_null"] else (custom_field["valueText"] or custom_field["valueJson"])
        )

    prices_by_product: Dict[int, Dict[str, object]] = {}
    if include_price_lists and price_list_headers:
        cur.execute(
            """
            SELECT productId, priceListId, COALESCE(currencyCode, '') AS currencyCode, value
            FROM ref_price_list_values
            ORDER BY productId
            """
        )
        for price_row in cur.fetchall():
            product_id = price_row["productId"]
            if product_id is None:
                continue
            header = price_list_key_to_header.get(
                (int(price_row["priceListId"]), str(price_row["currencyCode"] or ""))
            )
            if not header:
                continue
            prices_by_product.setdefault(int(product_id), {})[header] = price_row["value"]

    suppliers_by_product: Dict[int, str] = {}
    if include_suppliers:
        cur.execute(
            """
            SELECT productId, supplierContactId
            FROM export_product_suppliers
            ORDER BY productId, supplierContactId
            """
        )
        suppliers_list_by_product: Dict[int, List[str]] = {}
        for product_id, supplier_contact_id in cur.fetchall():
            if product_id is None:
                continue
            supplier_name = _resolve_reference_name(supplier_lookup, supplier_contact_id)
            if not supplier_name:
                continue
            suppliers_list_by_product.setdefault(int(product_id), []).append(supplier_name)
        suppliers_by_product = {
            pid: " | ".join(names) for pid, names in suppliers_list_by_product.items()
        }

    with open_table(file_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()

        for product in products:
            product_id = product["productId"]
            if product_id is None:
                continue
            product_id_int = int(product_id)
            row = {key: product[key] for key in product.keys() if key in headers}
            row["brandId"] = _resolve_reference_name(brand_lookup, product["brandId"])
            row["collectionId"] = _resolve_reference_name(collection_lookup, product["collectionId"])
            row["productTypeId"] = _resolve_reference_name(product_type_lookup, product["productTypeId"])
            row["productGroupId"] = _resolve_reference_name(product_group_lookup, product["productGroupId"])
            row["reportingCategoryId"] = _resolve_reference_name(
                category_lookup, product["reportingCategoryId"]
            )
            row["reportingSubcategoryId"] = _resolve_reference_name(
                category_lookup, product["reportingSubcategoryId"]
            )
            row["reportingSeasonId"] = _resolve_reference_name(season_lookup, product["reportingSeasonId"])
            row["primarySupplierId"] = _resolve_reference_name(
                supplier_lookup, product["primarySupplierId"]
            )

            row["seasonIds"] = json.dumps(
                [
                    _resolve_reference_name(season_lookup, season_id)
                    for season_id in seasons_by_product.get(product_id_int, [])
                ]
            )

            sales_channels = sales_channels_by_product.get(product_id_int, [])

            def _join_channel_values(key: str) -> str:
                values = []
                for channel in sales_channels:
                    value = channel[key]
                    if value is None:
                        continue
                    text = str(value).strip()
                    if text:
                        values.append(text)
                return " | ".join(values)

            row["salesChannelName"] = _join_channel_values("salesChannelName")
            row["productName"] = _join_channel_values("productName")
            row["productCondition"] = _join_channel_values("productCondition")
            row["descriptionLanguageCode"] = _join_channel_values("descriptionLanguageCode")
            row["descriptionText"] = _join_channel_values("descriptionText")
            row["shortDescriptionLanguageCode"] = _join_channel_values("shortDescriptionLanguageCode")
            row["shortDescriptionText"] = _join_channel_values("shortDescriptionText")

            category_codes: List[str] = []
            category_names: List[str] = []
            for r in categories_by_product.get(product_id_int, []):
                category_code = str(r["categoryCode"]) if r["categoryCode"] is not None else ""
                if category_code:
                    category_codes.append(category_code)
                category_name = _resolve_reference_name(category_lookup, r["categoryCode"])
                if category_name:
                    category_names.append(category_name)
            row["categoryCode"] = " | ".join(category_codes)
            row["categoryName"] = " | ".join(category_names)

            variations = []
            for r in variations_by_product.get(product_id_int, []):
                option_id = r["optionId"]
                option_value_id = r["optionValueId"]
                option_name = _resolve_reference_name(option_lookup, option_id)
                option_value_name = ""
                lookup_key = None
                try:
                    if option_id is not None and option_value_id is not None:
                        lookup_key = (int(option_id), int(option_value_id))
                except (TypeError, ValueError):
                    lookup_key = None
                if lookup_key and lookup_key in option_value_lookup:
                    option_value_name = option_value_lookup[lookup_key]
                elif option_value_id is not None:
                    option_value_name = str(option_value_id)
                variations.append(
                    {
                        "optionId": option_name,
                        "optionValueId": option_value_name,
                    }
                )
            row["variations"] = json.dumps(variations)

            bundle_components = []
            for r in bundle_components_by_product.get(product_id_int, []):
                component_id = r["componentProductId"]
                component_name = str(component_id)
                try:
                    if component_id is not None:
                        component_name = component_name_lookup.get(int(component_id), str(component_id))
                except (TypeError, ValueError):
                    component_name = str(component_id)
                bundle_components.append(
                    {
                        "componentProductId": component_name,
                        "componentQty": r["componentQty"],
                    }
                )
            row["bundleComponents"] = json.dumps(bundle_components)

            warehouses = []
            for r in warehouses_by_product.get(product_id_int, []):
                warehouses.append(
                    {
                        "warehouseId": _resolve_reference_name(warehouse_lookup, r["warehouseId"]),
                        "defaultLocationId": r["defaultLocationId"],
                        "reorderLevel": r["reorderLevel"],
                        "reorderQuantity": r["reorderQuantity"],
                    }
                )
            row["warehouses"] = json.dumps(warehouses)

            custom_field_map = custom_fields_by_product.get(product_id_int, {})
            for code in custom_field_codes:
                row[f"customFields.{code}"] = custom_field_map.get(code, "")

            if include_price_lists and price_list_headers:
                for header in price_list_headers:
                    row[header] = ""
                for header, value in prices_by_product.get(product_id_int, {}).items():
                    row[header] = value

            if include_suppliers:
                row["suppliers"] = suppliers_by_product.get(product_id_int, "")

            writer.writerow(row)

    conn.close()
    return len(products)


__all__ = [
    "export_synced_product_catalogue_to_csv",
    "sync_export_product_suppliers",
    "sync_product_catalogue_export_data",
]
