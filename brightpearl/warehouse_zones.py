"""Warehouse zone validation and synchronisation helpers."""
from __future__ import annotations

import csv
from csv_safety import open_csv
import json
import os
import re
import sqlite3
from typing import List, Optional, Sequence, Tuple

import requests

from .common import (
    processing_column_definitions,
    ensure_processing_columns,
    mark_rows_processed,
    unprocessed_where_clause,
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    log,
    log_search_progress,
    log_payload_exchange,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)
from .performance import estimate_record_count, record_api_update
from .settings import get_settings, get_upload_retry_settings

UNMATCHED_DIR = "output/unmatched"

VALIDATED_TABLE = "validated_warehouse_zones"
REF_ZONES = "ref_zones"

CATALOGUE_COLUMNS: Sequence[str] = ("zoneId", "name", "warehouseId")
TEMPLATE_HEADERS: Sequence[str] = ("name", "warehouseId")


def _safe_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ensure_ref_zones_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REF_ZONES} (
            zoneId INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE,
            warehouseId INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_{REF_ZONES}_warehouse_name
        ON {REF_ZONES}(warehouseId, name)
        """
    )
    conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(
        f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            warehouseId INTEGER NOT NULL,
            original_row_json TEXT{processing_column_definitions()}
        )
        """
    )
    _ensure_ref_zones_table(conn)


def _entry_as_mapping(entry) -> dict:
    if isinstance(entry, dict):
        return entry

    if isinstance(entry, (list, tuple)):
        mapped = {}
        for index, column in enumerate(CATALOGUE_COLUMNS):
            if index < len(entry):
                mapped[column] = entry[index]
        return mapped

    return {}


def _zone_record_from_catalogue_entry(entry) -> Optional[Tuple[int, str, int]]:
    entry_dict = _entry_as_mapping(entry)
    zone_id = _safe_int(entry_dict.get("zoneId"))
    name = _safe_text(entry_dict.get("name"))
    warehouse_id = _safe_int(entry_dict.get("warehouseId"))
    if zone_id is None or name is None or warehouse_id is None:
        return None
    return (zone_id, name, warehouse_id)


def update_zone_catalogue(
    account_name: str,
    db_path: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Fetch the warehouse zone catalogue used to detect duplicates."""

    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    api_url_catalogue = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "warehouse-service/zone-search"
    )

    first_result = 1
    more_pages_available = True
    all_zones = []
    zone_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(
                f"🛑 Cancelled after syncing {zone_counter} zones (so far).",
                log_callback,
            )
            break

        paginated_url = f"{api_url_catalogue}?pageSize=500&firstResult={first_result}"
        json_response, next_throttle_period, requests_remaining = send_request(
            paginated_url,
            credentials.headers,
            cancel_token=cancel_token,
            log_callback=log_callback,
        )

        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after request (so far: {zone_counter}).", log_callback)
            break

        if json_response:
            payload = json.loads(json_response)
            if isinstance(payload, dict):
                data = payload.get("response", payload)
            elif isinstance(payload, list):
                data = {}
                for element in payload:
                    if isinstance(element, dict):
                        data = element.get("response", element)
                        break
            else:
                data = {}

            if not isinstance(data, dict):
                log("⚠️ Unexpected payload for zone catalogue.", log_callback)
                more_pages_available = False
                continue

            results = data.get("results", [])
            all_zones.extend(results)
            zone_counter += len(results)
            record_api_update(len(results))
            log(f"📦 Synced {zone_counter} zones", log_callback)

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

    if not all_zones:
        if cancel_token and cancel_token.is_set():
            return zone_counter
        log("⚠️ No zones returned from Brightpearl.", log_callback)
        return zone_counter

    conn = sqlite3.connect(db_path)
    inserted = 0
    try:
        _ensure_ref_zones_table(conn)
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {REF_ZONES}")
        conn.commit()

        for entry in all_zones:
            if cancel_token and cancel_token.is_set():
                log("🛑 Cancelled during DB write; partial commit.", log_callback)
                break
            record = _zone_record_from_catalogue_entry(entry)
            if not record:
                continue
            cur.execute(
                f"""
                INSERT INTO {REF_ZONES} (zoneId, name, warehouseId)
                VALUES (?, ?, ?)
                """,
                record,
            )
            inserted += 1

        conn.commit()
    finally:
        conn.close()

    if cancel_token and cancel_token.is_set():
        log("🟡 Zone catalogue sync exited due to cancellation.", log_callback)
    else:
        log(f"✅ Saved {inserted} reference zones.", log_callback)

    return inserted


def _warehouse_to_id(cur: sqlite3.Cursor, warehouse_field: str) -> Optional[int]:
    value = (warehouse_field or "").strip()
    match = re.match(r"^(\d+)\b", value)
    if match:
        return int(match.group(1))

    cur.execute("SELECT warehouseId, name FROM ref_warehouses")
    for wid, name in cur.fetchall():
        if isinstance(name, str) and name.strip().lower() == value.lower():
            return int(wid)
    return None


def _zone_exists(conn: sqlite3.Connection, warehouse_id: int, zone_name: str) -> Optional[int]:
    cur = conn.cursor()
    cur.execute(
        f"SELECT zoneId FROM {REF_ZONES} WHERE warehouseId=? AND LOWER(name)=LOWER(?)",
        (warehouse_id, zone_name.strip()),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _write_problem_csv(
    account_name: str,
    filename: str,
    rows: List[dict],
    headers: Sequence[str],
    log_callback=None,
) -> None:
    if not rows:
        return
    os.makedirs(UNMATCHED_DIR, exist_ok=True)
    path = os.path.join(UNMATCHED_DIR, f"{account_name}_{filename}")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    log(f"⚠️ Wrote {len(rows)} rows → {path}", log_callback)


def validate_zones(
    csv_path: str,
    db_path: str,
    account_name: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Validate a zones CSV and persist rows ready for syncing."""

    ensure_account_binding(db_path, account_name)

    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()

        invalid_warehouses: List[dict] = []
        invalid_names: List[dict] = []
        duplicate_existing: List[dict] = []
        duplicate_csv: List[dict] = []
        inserted = 0
        headers: Sequence[str] = TEMPLATE_HEADERS

        with open_csv(csv_path) as csv_file:
            reader = csv.DictReader(csv_file)
            headers = reader.fieldnames or TEMPLATE_HEADERS

            seen_keys = set()
            for index, row in enumerate(reader, 1):
                if cancel_token and cancel_token.is_set():
                    log(
                        f"🛑 Cancelled during validation at row {index}. Inserted so far: {inserted}.",
                        log_callback,
                    )
                    break

                zone_name = (row.get("name") or "").strip()
                warehouse_name = (row.get("warehouseId") or "").strip()

                if not zone_name:
                    invalid_names.append(row)
                    continue

                warehouse_id = _warehouse_to_id(cur, warehouse_name)
                if warehouse_id is None:
                    invalid_warehouses.append(row)
                    continue

                if _zone_exists(conn, warehouse_id, zone_name):
                    duplicate_existing.append(row)
                    continue

                key = (warehouse_id, zone_name.lower())
                if key in seen_keys:
                    duplicate_csv.append(row)
                    continue
                seen_keys.add(key)

                cur.execute(
                    f"""
                    INSERT INTO {VALIDATED_TABLE} (name, warehouseId, original_row_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        zone_name,
                        warehouse_id,
                        json.dumps(row, ensure_ascii=False),
                    ),
                )
                inserted += 1

        conn.commit()

    finally:
        conn.close()

    log(f"✅ Inserted {inserted} validated zone(s).", log_callback)

    _write_problem_csv(account_name, "zones_invalid_warehouses.csv", invalid_warehouses, headers, log_callback)
    _write_problem_csv(account_name, "zones_missing_names.csv", invalid_names, headers, log_callback)
    _write_problem_csv(account_name, "zones_duplicate_existing.csv", duplicate_existing, headers, log_callback)
    _write_problem_csv(account_name, "zones_duplicate_csv.csv", duplicate_csv, headers, log_callback)

    return inserted


def _load_validated_rows(conn: sqlite3.Connection) -> List[dict]:
    cur = conn.cursor()
    ensure_processing_columns(conn, VALIDATED_TABLE)
    cur.execute(f"SELECT id, name, warehouseId FROM {VALIDATED_TABLE} WHERE {unprocessed_where_clause()}")
    rows = []
    for row_id, name, warehouse_id in cur.fetchall():
        rows.append({"id": row_id, "name": name, "warehouseId": warehouse_id})
    return rows


def _parse_int_header(headers: requests.structures.CaseInsensitiveDict, key: str, default: int = 0) -> int:
    try:
        return int(headers.get(key, default))
    except (TypeError, ValueError):
        return default


def _post_zone(
    url: str,
    headers: dict,
    payload: dict,
    *,
    log_callback=None,
    cancel_token=None,
) -> Tuple[bool, Optional[int], int, int]:
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            return False, None, 0, 0
        try:
            response = requests.post(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("POST", url, payload, response, log_callback)
            status = response.status_code
            log(f"POST {url} attempt {attempt} → {status}", log_callback)

            if status in (200, 201, 202):
                record_api_update(estimate_record_count(payload))
                created_id: Optional[int] = None
                try:
                    data = response.json()
                except ValueError:
                    data = {}

                try:
                    created_id = int(data["response"]["id"])
                except Exception:
                    created_id = None

                requests_remaining = _parse_int_header(
                    response.headers, "brightpearl-requests-remaining", 3
                )
                throttle_ms = _parse_int_header(
                    response.headers, "brightpearl-next-throttle-period", 0
                )

                if created_id is None:
                    snippet = json.dumps(data, ensure_ascii=False)[:300]
                    log(
                        f"ℹ️ 200-series response but no numeric id parsed. Body snippet: {snippet}",
                        log_callback,
                    )

                return True, created_id, requests_remaining, throttle_ms

            log(f"❌ {status}: {response.text[:400]}", log_callback)

        except requests.RequestException as exc:
            log(f"❌ request error: {exc}", log_callback)

        sleep_ms = default_sleep_ms * attempt
        sleep_with_cancel_ms(sleep_ms, cancel_token, log_callback)

    return False, None, 0, 0


def _upsert_zone_ref(conn: sqlite3.Connection, zone_id: int, name: str, warehouse_id: int) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT OR REPLACE INTO {REF_ZONES} (zoneId, name, warehouseId)
        VALUES (?, ?, ?)
        """,
        (zone_id, name, warehouse_id),
    )
    conn.commit()


def sync_zones(
    account_name: str,
    db_path: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Sync validated zones to Brightpearl."""

    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    conn = sqlite3.connect(db_path)
    created_count = 0
    try:
        rows = _load_validated_rows(conn)
        total = len(rows)
        log(f"🔄 Syncing {total} zone(s)…", log_callback)

        headers = {**credentials.headers, "Content-Type": "application/json"}

        for idx, row in enumerate(rows, 1):
            if cancel_token and cancel_token.is_set():
                log(
                    f"🛑 Sync cancelled after processing {idx - 1} of {total} rows.",
                    log_callback,
                )
                break

            warehouse_id = row["warehouseId"]
            zone_name = row["name"]

            existing_id = _zone_exists(conn, warehouse_id, zone_name)
            if existing_id:
                mark_rows_processed(conn, VALIDATED_TABLE, row["id"], f"Zone already exists {existing_id}")
                conn.commit()
                log(
                    f"⏭️ Skipping existing zone (warehouse {warehouse_id}) → {existing_id} :: {zone_name}",
                    log_callback,
                )
                progress = int((idx / max(total, 1)) * 100)
                log(f"PROGRESS:{progress}", log_callback)
                continue

            url = (
                f"https://{credentials.region}.brightpearlconnect.com/public-api/"
                f"{account_name}/warehouse-service/warehouse/{warehouse_id}/zone/"
            )

            ok, created_id, requests_remaining, throttle_ms = _post_zone(
                url,
                headers,
                {"name": zone_name},
                log_callback=log_callback,
                cancel_token=cancel_token,
            )

            if ok:
                mark_rows_processed(conn, VALIDATED_TABLE, row["id"], f"Created zone {created_id or zone_name}")
                conn.commit()
                if created_id is not None:
                    _upsert_zone_ref(conn, created_id, zone_name, warehouse_id)
                created_count += 1
                log(
                    f"✅ Zone created (WH {warehouse_id}) → {created_id or 'OK'} :: {zone_name}",
                    log_callback,
                )
            else:
                log(
                    f"❌ Failed to create zone (WH {warehouse_id}) :: {zone_name}",
                    log_callback,
                )

            if cancel_token and cancel_token.is_set():
                break

            if idx < total:
                if should_pause_for_throttle(requests_remaining, throttle_ms):
                    sleep_with_cancel_ms(throttle_ms, cancel_token, log_callback)
                else:
                    sleep_with_cancel_ms(get_upload_retry_settings()[1], cancel_token, log_callback)

            progress = int((idx / max(total, 1)) * 100)
            log(f"PROGRESS:{progress}", log_callback)

    finally:
        conn.close()

    if cancel_token and cancel_token.is_set():
        log("🟡 Sync exited early due to cancellation.", log_callback)
    else:
        log("✅ Zones sync complete.", log_callback)

    return created_count


__all__ = [
    "TEMPLATE_HEADERS",
    "VALIDATED_TABLE",
    "update_zone_catalogue",
    "validate_zones",
    "sync_zones",
]
