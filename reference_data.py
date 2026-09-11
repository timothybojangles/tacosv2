"""Fetch and persist Brightpearl reference data used by the tooling UI."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any, Dict, Iterable, Optional, Sequence

from brightpearl.common import (
    connect_sqlite,
    ensure_account_binding,
    log,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)
from brightpearl.performance import estimate_record_count, record_api_update


def ensure_reference_tables(db_path: str) -> None:
    """Create the SQLite tables that hold Brightpearl reference lookups."""

    conn = connect_sqlite(db_path)
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
        """
        CREATE TABLE IF NOT EXISTS ref_shipping_methods (
            shippingMethodId INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_currencies (
            currencyId INTEGER PRIMARY KEY,
            isoCode TEXT UNIQUE,
            name TEXT,
            symbol TEXT,
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_channels (
            channelId INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_price_lists (
            priceListId INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_order_statuses (
            statusId INTEGER PRIMARY KEY,
            name TEXT,
            code TEXT,
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_company_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_configuration (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_payment_methods (
            paymentMethodId INTEGER PRIMARY KEY,
            code TEXT,
            name TEXT,
            rawJson TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ref_taxcode (
            taxCodeId INTEGER PRIMARY KEY,
            code TEXT,
            description TEXT,
            rate REAL,
            rawJson TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def _fetch_endpoint(
    name: str,
    url: str,
    headers: dict,
    log_callback=None,
    cancel_token=None,
) -> Optional[object]:
    """Fetch ``url`` and return the parsed ``response`` payload."""

    if cancel_token and cancel_token.is_set():
        return None

    json_response, throttle_ms, requests_remaining = send_request(
        url, headers, cancel_token=cancel_token, log_callback=log_callback
    )

    if cancel_token and cancel_token.is_set():
        return None

    if not json_response:
        log(f"⚠️ No response returned for {name}.", log_callback)
        return None

    try:
        payload = json.loads(json_response).get("response")
    except json.JSONDecodeError as exc:
        log(f"⚠️ Failed to parse {name} payload: {exc}", log_callback)
        payload = None

    if payload is not None:
        record_api_update(estimate_record_count(payload))

    if should_pause_for_throttle(requests_remaining, throttle_ms):
        log(
            f"⏳ Throttling after {name} request; sleeping {throttle_ms} ms.",
            log_callback,
        )
        sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)

    return payload


def _store_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: Iterable[str],
    rows: Iterable[Iterable],
) -> int:
    placeholders = ",".join(["?"] * len(columns))
    column_list = ",".join(columns)
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM {table}")

    inserted = 0
    for row in rows:
        if row is None:
            continue
        cursor.execute(
            f"INSERT OR REPLACE INTO {table} ({column_list}) VALUES ({placeholders})",
            tuple(row),
        )
        inserted += 1

    return inserted


def _as_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":")) if value is not None else "null"


def _coerce_scalar(value):
    """Ensure SQLite parameters are simple scalars or ``None``."""

    if value is None or isinstance(value, (str, int, float)):
        return value
    return _as_json(value)


def _extract_name_text(value: Any) -> Any:
    """Extract nested name text fields when API returns a structured name object."""

    if isinstance(value, dict):
        text_value = value.get("text")
        if text_value is not None:
            return text_value
    return value


def _seq_get(row: Sequence[Any], index: int) -> Any:
    """Safely fetch ``index`` from ``row`` when it's a non-string sequence."""

    if isinstance(row, (str, bytes)):
        return None
    if index < len(row):
        return row[index]
    return None


def _extract_sequence(payload: Optional[object], *preferred_keys: str) -> Iterable[Any]:
    """Return an iterable of result rows regardless of payload shape."""

    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in preferred_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for key in ("results", "data", "items", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _normalise_warehouse(row: Any) -> Optional[Iterable[Any]]:
    if isinstance(row, dict):
        warehouse_id = row.get("warehouseId") or row.get("id")
        name = _coerce_scalar(row.get("name"))
        code = _coerce_scalar(row.get("code") or row.get("type"))
    elif isinstance(row, Sequence):
        warehouse_id = _seq_get(row, 0)
        name = _coerce_scalar(_seq_get(row, 1))
        code = _coerce_scalar(_seq_get(row, 2))
    else:
        return None

    if warehouse_id is None:
        return None

    return (
        warehouse_id,
        name,
        code,
        _as_json(row),
    )


def _normalise_shipping_method(row: Any) -> Optional[Iterable[Any]]:
    if isinstance(row, dict):
        method_id = row.get("shippingMethodId") or row.get("id")
        name = _coerce_scalar(row.get("name"))
        code = _coerce_scalar(row.get("code") or row.get("reference"))
    elif isinstance(row, Sequence):
        method_id = _seq_get(row, 0)
        name = _coerce_scalar(_seq_get(row, 1))
        code = _coerce_scalar(_seq_get(row, 2))
    else:
        return None

    if method_id is None:
        return None

    return (
        method_id,
        name,
        code,
        _as_json(row),
    )


def _normalise_currency(row: Any) -> Optional[Iterable[Any]]:
    if isinstance(row, dict):
        currency_id = row.get("currencyId") or row.get("id")
        iso_code = _coerce_scalar(row.get("isoCode") or row.get("code"))
        name = _coerce_scalar(row.get("name"))
        symbol = _coerce_scalar(row.get("symbol"))
    elif isinstance(row, Sequence):
        currency_id = _seq_get(row, 0)
        iso_code = None
        name = None
        symbol = None

        for value in row[1:]:
            if isinstance(value, str):
                if iso_code is None and value.isupper() and 3 <= len(value) <= 4:
                    iso_code = value
                    continue
                if symbol is None and any(not ch.isalnum() for ch in value):
                    symbol = value
                    continue
                if name is None:
                    name = value

        iso_code = _coerce_scalar(iso_code if iso_code is not None else _seq_get(row, 1))
        name = _coerce_scalar(name if name is not None else _seq_get(row, 2))
        symbol = _coerce_scalar(symbol if symbol is not None else _seq_get(row, 3))
    else:
        return None

    if currency_id is None:
        return None

    return (
        currency_id,
        iso_code,
        name,
        symbol,
        _as_json(row),
    )


def _normalise_payment_method(row: Any) -> Optional[Iterable[Any]]:
    if isinstance(row, dict):
        method_id = row.get("paymentMethodId") or row.get("id")
        name = _coerce_scalar(row.get("name"))
        code = _coerce_scalar(row.get("code") or row.get("reference"))
    elif isinstance(row, Sequence):
        method_id = _seq_get(row, 0)
        code = _coerce_scalar(_seq_get(row, 1))
        name = _coerce_scalar(_seq_get(row, 2))
    else:
        return None

    if method_id is None:
        return None

    return (
        method_id,
        code,
        name,
        _as_json(row),
    )


def fetch_and_store_reference_tables(
    account_name: str,
    region: str,
    headers: Dict[str, str],
    db_path: str,
    reference_keys: Optional[Sequence[str]] = None,
    log_callback=None,
    cancel_token=None,
) -> Dict[str, int]:
    """Fetch Brightpearl reference lookups and persist them in SQLite.

    Returns a dictionary mapping each reference key to the number of rows stored.
    Scalar payloads such as company profile/configuration will return ``0`` or ``1``
    depending on whether data was stored.
    """

    ensure_account_binding(db_path, account_name)
    ensure_reference_tables(db_path)

    base = f"https://{region}.brightpearlconnect.com/public-api/{account_name}"
    endpoints = {
        "warehouses": f"{base}/warehouse-service/warehouse-search",
        "shipping_methods": f"{base}/warehouse-service/shipping-method-search",
        "currencies": f"{base}/accounting-service/currency-search",
        "channels": f"{base}/product-service/channel",
        "price_lists": f"{base}/product-service/price-list",
        "order_statuses": f"{base}/order-service/order-status",
        "company": f"{base}/product-service/channel-brand/default",
        "configuration": f"{base}/integration-service/account-configuration",
        "payment_methods": f"{base}/accounting-service/payment-method-search",
        "tax_codes": f"{base}/accounting-service/tax-code/",
    }
    selected_keys = set(reference_keys) if reference_keys else set(endpoints.keys())
    unknown_keys = sorted(selected_keys.difference(endpoints.keys()))
    if unknown_keys:
        log(
            f"⚠️ Unknown reference keys requested: {', '.join(unknown_keys)}",
            log_callback,
        )
    endpoints = {key: url for key, url in endpoints.items() if key in selected_keys}

    results: Dict[str, int] = {}
    payloads: Dict[str, Optional[object]] = {}

    total = max(1, len(endpoints))
    for index, (key, url) in enumerate(endpoints.items(), start=1):
        if cancel_token and cancel_token.is_set():
            log("🛑 Reference data sync cancelled.", log_callback)
            break
        log(f"➡️ Fetching {key.replace('_', ' ')}...", log_callback)
        payloads[key] = _fetch_endpoint(key, url, headers, log_callback, cancel_token)
        progress = int((index / total) * 100)
        log(f"PROGRESS:{progress}", log_callback)

    if cancel_token and cancel_token.is_set():
        return results

    conn = connect_sqlite(db_path)

    try:
        # Warehouses
        if "warehouses" in endpoints:
            warehouses = _extract_sequence(payloads.get("warehouses"), "warehouses")
            results["warehouses"] = _store_rows(
                conn,
                "ref_warehouses",
                ("warehouseId", "name", "code", "rawJson"),
                (
                    normalised
                    for row in warehouses
                    for normalised in (_normalise_warehouse(row),)
                    if normalised is not None
                ),
            )

        # Shipping methods
        if "shipping_methods" in endpoints:
            shipping_methods = _extract_sequence(
                payloads.get("shipping_methods"), "shippingMethods"
            )
            results["shipping_methods"] = _store_rows(
                conn,
                "ref_shipping_methods",
                ("shippingMethodId", "name", "code", "rawJson"),
                (
                    normalised
                    for row in shipping_methods
                    for normalised in (_normalise_shipping_method(row),)
                    if normalised is not None
                ),
            )

        # Currencies
        if "currencies" in endpoints:
            currencies = _extract_sequence(payloads.get("currencies"), "currencies")
            results["currencies"] = _store_rows(
                conn,
                "ref_currencies",
                ("currencyId", "isoCode", "name", "symbol", "rawJson"),
                (
                    normalised
                    for row in currencies
                    for normalised in (_normalise_currency(row),)
                    if normalised is not None
                ),
            )

        # Channels
        if "channels" in endpoints:
            channels = payloads.get("channels") or []
            results["channels"] = _store_rows(
                conn,
                "ref_channels",
                ("channelId", "name", "code", "rawJson"),
                (
                    (
                        row.get("channelId") or row.get("id"),
                        _coerce_scalar(row.get("name")),
                        _coerce_scalar(row.get("code")),
                        _as_json(row),
                    )
                    for row in channels
                    if isinstance(row, dict)
                    and (row.get("channelId") or row.get("id")) is not None
                ),
            )

        # Price lists
        if "price_lists" in endpoints:
            price_lists = payloads.get("price_lists") or []
            results["price_lists"] = _store_rows(
                conn,
                "ref_price_lists",
                ("priceListId", "name", "code", "rawJson"),
                (
                    (
                        row.get("priceListId") or row.get("id"),
                        _coerce_scalar(_extract_name_text(row.get("name"))),
                        _coerce_scalar(row.get("code")),
                        _as_json(row),
                    )
                    for row in price_lists
                    if isinstance(row, dict)
                    and (row.get("priceListId") or row.get("id")) is not None
                ),
            )

        # Order statuses
        if "order_statuses" in endpoints:
            statuses = payloads.get("order_statuses") or []
            results["order_statuses"] = _store_rows(
                conn,
                "ref_order_statuses",
                ("statusId", "name", "code", "rawJson"),
                (
                    (
                        row.get("statusId")
                        or row.get("orderStatusId")
                        or row.get("id"),
                        _coerce_scalar(row.get("name")),
                        _coerce_scalar(row.get("code")),
                        _as_json(row),
                    )
                    for row in statuses
                    if isinstance(row, dict)
                    and (
                        row.get("statusId")
                        or row.get("orderStatusId")
                        or row.get("id")
                    )
                    is not None
                ),
            )

        # Company profile (single row)
        if "company" in endpoints:
            company_profile = payloads.get("company")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ref_company_profile")
            if company_profile is not None:
                cursor.execute(
                    "INSERT INTO ref_company_profile (id, rawJson) VALUES (1, ?)",
                    (_as_json(company_profile),),
                )
                results["company"] = 1
            else:
                results["company"] = 0

        # Configuration (single row)
        if "configuration" in endpoints:
            configuration = payloads.get("configuration")
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ref_configuration")
            if configuration is not None:
                cursor.execute(
                    "INSERT INTO ref_configuration (id, rawJson) VALUES (1, ?)",
                    (_as_json(configuration),),
                )
                results["configuration"] = 1
            else:
                results["configuration"] = 0

        # Payment methods
        if "payment_methods" in endpoints:
            payment_methods = _extract_sequence(
                payloads.get("payment_methods"), "paymentMethods"
            )
            results["payment_methods"] = _store_rows(
                conn,
                "ref_payment_methods",
                ("paymentMethodId", "code", "name", "rawJson"),
                (
                    normalised
                    for row in payment_methods
                    for normalised in (_normalise_payment_method(row),)
                    if normalised is not None
                ),
            )

        # Tax codes
        if "tax_codes" in endpoints:
            tax_codes = _extract_sequence(payloads.get("tax_codes"))
            results["tax_codes"] = _store_rows(
                conn,
                "ref_taxcode",
                ("taxCodeId", "code", "description", "rate", "rawJson"),
                (
                    (
                        row.get("id"),
                        _coerce_scalar(row.get("code")),
                        _coerce_scalar(row.get("description")),
                        _coerce_scalar(row.get("rate")),
                        _as_json(row),
                    )
                    for row in tax_codes
                    if isinstance(row, dict) and row.get("id") is not None
                ),
            )

        conn.commit()
    finally:
        conn.close()

    for key, count in results.items():
        log(f"✅ Stored {count} rows for {key}.", log_callback)

    return results
