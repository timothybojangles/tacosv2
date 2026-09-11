"""Warehouse location validation and synchronisation helpers."""
from __future__ import annotations

import csv
from csv_safety import open_csv, open_table
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

VALIDATED_TABLE = "validated_warehouse_locations"
UPDATE_TABLE = "location_updates"
REF_ZONES = "ref_zones"
REF_LOCATIONS = "ref_locations"

CATALOGUE_COLUMNS: Sequence[str] = (
    "locationId",
    "warehouseId",
    "zoneId",
    "groupingA",
    "groupingB",
    "groupingC",
    "groupingD",
    "barcode",
)

BARCODE_MAXLEN = 32
TEMPLATE_HEADERS: Sequence[str] = (
    "warehouse",
    "zone",
    "aisle",
    "bay",
    "shelf",
    "bin",
    "barcode",
)
UPDATE_HEADERS: Sequence[str] = (
    "locationId",
    "warehouse",
    "zone",
    "aisle",
    "bay",
    "shelf",
    "bin",
    "barcode",
)


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


def _ensure_ref_locations_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REF_LOCATIONS} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            locationId INTEGER,
            warehouseId INTEGER NOT NULL,
            zoneId INTEGER,
            groupingA TEXT NOT NULL,
            groupingB TEXT,
            groupingC TEXT,
            groupingD TEXT,
            barcode TEXT
        )
        """
    )

    cur.execute(f"PRAGMA table_info({REF_LOCATIONS})")
    columns = {row[1] for row in cur.fetchall()}
    if "locationId" not in columns:
        cur.execute(f"ALTER TABLE {REF_LOCATIONS} ADD COLUMN locationId INTEGER")

    cur.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_{REF_LOCATIONS}_key
        ON {REF_LOCATIONS}(
            warehouseId,
            IFNULL(zoneId, -1),
            groupingA,
            IFNULL(groupingB, ''),
            IFNULL(groupingC, ''),
            IFNULL(groupingD, '')
        )
        """
    )

    cur.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_{REF_LOCATIONS}_barcode
        ON {REF_LOCATIONS}(barcode)
        """
    )

    conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure the local tables used during validation exist."""

    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(
        f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            warehouse TEXT NOT NULL,
            warehouseId INTEGER NOT NULL,
            zone TEXT,
            zoneId INTEGER,
            groupingA TEXT NOT NULL,
            groupingB TEXT,
            groupingC TEXT,
            groupingD TEXT,
            barcode TEXT,
            original_row_json TEXT{processing_column_definitions()}
        )
        """
    )

    _ensure_ref_locations_table(conn)


def _ensure_update_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {UPDATE_TABLE}")
    cur.execute(
        f"""
        CREATE TABLE {UPDATE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            locationId INTEGER NOT NULL,
            warehouseId INTEGER NOT NULL,
            zone TEXT,
            zoneId INTEGER,
            groupingA TEXT NOT NULL,
            groupingB TEXT,
            groupingC TEXT,
            groupingD TEXT,
            barcode TEXT,
            original_row_json TEXT{processing_column_definitions()}
        )
        """
    )
    _ensure_ref_locations_table(conn)


def _entry_as_mapping(entry) -> dict:
    """Return ``entry`` as a ``dict`` regardless of Brightpearl response shape."""

    if isinstance(entry, dict):
        return entry

    if isinstance(entry, (list, tuple)):
        mapped = {}
        for index, column in enumerate(CATALOGUE_COLUMNS):
            if index < len(entry):
                mapped[column] = entry[index]
        return mapped

    return {}


def _location_record_from_catalogue_entry(entry) -> Optional[Tuple]:
    entry_dict = _entry_as_mapping(entry)

    location_id = _safe_int(entry_dict.get("locationId"))
    warehouse_id = _safe_int(entry_dict.get("warehouseId"))
    grouping_a = _safe_text(entry_dict.get("groupingA"))
    if warehouse_id is None or grouping_a is None:
        return None

    return (
        location_id,
        warehouse_id,
        _safe_int(entry_dict.get("zoneId")),
        grouping_a,
        _safe_text(entry_dict.get("groupingB")),
        _safe_text(entry_dict.get("groupingC")),
        _safe_text(entry_dict.get("groupingD")),
        _safe_text(entry_dict.get("barcode")),
    )


def update_location_catalogue(
    account_name: str,
    db_path: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Fetch the warehouse location catalogue used to detect duplicates."""

    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    api_url_catalogue = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "warehouse-service/location-search"
    )

    first_result = 1
    more_pages_available = True
    all_locations = []
    location_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(
                f"🛑 Cancelled after syncing {location_counter} locations (so far).",
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
            log(
                f"🛑 Cancelled after request (so far: {location_counter}).",
                log_callback,
            )
            break

        if json_response:
            payload = json.loads(json_response)
            if isinstance(payload, dict):
                data = payload.get("response", payload)
            elif isinstance(payload, list):
                data = {}
                for element in payload:
                    if isinstance(element, dict):
                        if "response" in element:
                            data = element["response"]
                        else:
                            data = element
                        break
            else:
                data = {}

            if not isinstance(data, dict):
                log("⚠️ Unexpected payload for location catalogue.", log_callback)
                more_pages_available = False
                continue

            results = data.get("results", [])
            all_locations.extend(results)
            location_counter += len(results)
            record_api_update(len(results))
            log(f"📦 Synced {location_counter} locations", log_callback)

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

    if not all_locations:
        if cancel_token and cancel_token.is_set():
            return location_counter
        log("⚠️ No locations returned from Brightpearl.", log_callback)
        return location_counter

    conn = sqlite3.connect(db_path)
    inserted = 0
    try:
        _ensure_ref_locations_table(conn)
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {REF_LOCATIONS}")
        conn.commit()

        for entry in all_locations:
            if cancel_token and cancel_token.is_set():
                log("🛑 Cancelled during DB write; partial commit.", log_callback)
                break
            record = _location_record_from_catalogue_entry(entry)
            if not record:
                continue
            cur.execute(
                f"""
                INSERT INTO {REF_LOCATIONS}
                (
                    locationId,
                    warehouseId,
                    zoneId,
                    groupingA,
                    groupingB,
                    groupingC,
                    groupingD,
                    barcode
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                record,
            )
            inserted += 1

        conn.commit()
    finally:
        conn.close()

    if cancel_token and cancel_token.is_set():
        log("🟡 Location catalogue sync exited due to cancellation.", log_callback)
    else:
        log(f"✅ Saved {inserted} reference locations.", log_callback)

    return inserted


def _warehouse_to_id(cur: sqlite3.Cursor, warehouse_field: str) -> Optional[int]:
    """Resolve ``warehouse_field`` to a warehouse identifier."""

    value = (warehouse_field or "").strip()
    match = re.match(r"^(\d+)\b", value)
    if match:
        return int(match.group(1))

    cur.execute("SELECT warehouseId, name FROM ref_warehouses")
    for wid, name in cur.fetchall():
        if isinstance(name, str) and name.strip().lower() == value.lower():
            return int(wid)
    return None


def _zone_to_id(cur: sqlite3.Cursor, warehouse_id: int, zone_name: str) -> Optional[int]:
    if not zone_name:
        return None
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


def _write_unmatched_csv(
    account_name: str,
    filename: str,
    rows: List[dict],
    log_callback=None,
) -> None:
    if not rows:
        return
    unmatched_dir = get_settings().unmatched_output_dir
    if not unmatched_dir:
        return
    os.makedirs(unmatched_dir, exist_ok=True)
    path = os.path.join(unmatched_dir, f"{account_name}_{filename}")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    log(f"⚠️ Wrote {len(rows)} rows → {path}", log_callback)


def _validate_groupings(
    grouping_a: str,
    grouping_b: str,
    grouping_c: str,
    grouping_d: str,
) -> Optional[str]:
    if grouping_d and not grouping_c:
        return "hierarchy"
    if grouping_c and not grouping_b:
        return "hierarchy"
    if grouping_b and not grouping_a:
        return "hierarchy"
    if not grouping_a:
        return "hierarchy"
    if any(
        value and not value.isalnum()
        for value in (grouping_a, grouping_b, grouping_c, grouping_d)
    ):
        return "alphanumeric"
    return None


def _validate_barcode(
    barcode: str,
    existing_barcodes,
    csv_barcodes: set,
    *,
    allowed_key: Optional[Tuple[int, int]] = None,
) -> Optional[str]:
    if not barcode:
        return None
    if (not barcode.isascii() and barcode.isalnum()) or len(barcode) > BARCODE_MAXLEN:
        return "Barcode must be ASCII and <= 32 characters."
    if isinstance(existing_barcodes, dict):
        existing_key = existing_barcodes.get(barcode)
        if existing_key and allowed_key and existing_key == allowed_key:
            return None
        if existing_key:
            return "Duplicate barcode detected."
    else:
        if barcode in existing_barcodes:
            return "Duplicate barcode detected."
    if barcode in csv_barcodes:
        return "Duplicate barcode detected."
    return None


def validate_locations(
    csv_path: str,
    db_path: str,
    account_name: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Validate a locations CSV and persist rows ready for syncing."""

    ensure_account_binding(db_path, account_name)

    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()

        invalid_warehouses: List[dict] = []
        invalid_zones: List[dict] = []
        invalid_hierarchy: List[dict] = []
        invalid_groupings: List[dict] = []
        invalid_barcodes: List[dict] = []
        duplicate_locations: List[dict] = []
        duplicate_barcodes: List[dict] = []
        inserted = 0
        headers: Sequence[str] = TEMPLATE_HEADERS

        with open_csv(csv_path) as csv_file:
            reader = csv.DictReader(csv_file)
            headers = reader.fieldnames or TEMPLATE_HEADERS

            existing_barcodes = set()
            cur.execute(
                f"SELECT barcode FROM {REF_LOCATIONS} WHERE barcode IS NOT NULL"
            )
            for (barcode,) in cur.fetchall():
                existing_barcodes.add(barcode)

            csv_barcodes = set()
            seen_keys = set()
            for index, row in enumerate(reader, 1):
                if cancel_token and cancel_token.is_set():
                    log(
                        f"🛑 Cancelled during validation at row {index}. Inserted so far: {inserted}.",
                        log_callback,
                    )
                    break

                warehouse_name = (row.get("warehouse") or "").strip()
                zone_name = (row.get("zone") or "").strip()
                grouping_a = (row.get("aisle") or "").strip()
                grouping_b = (row.get("bay") or "").strip()
                grouping_c = (row.get("shelf") or "").strip()
                grouping_d = (row.get("bin") or "").strip()
                barcode = (row.get("barcode") or "").strip()

                warehouse_id = _warehouse_to_id(cur, warehouse_name)
                if warehouse_id is None:
                    invalid_warehouses.append(row)
                    continue

                zone_id = _zone_to_id(cur, warehouse_id, zone_name) if zone_name else None
                if zone_name and zone_id is None:
                    invalid_zones.append(row)
                    continue

                grouping_error = _validate_groupings(
                    grouping_a,
                    grouping_b,
                    grouping_c,
                    grouping_d,
                )
                if grouping_error == "hierarchy":
                    invalid_hierarchy.append(row)
                    continue

                if grouping_error == "alphanumeric":
                    invalid_groupings.append(row)
                    continue

                barcode_error = _validate_barcode(barcode, existing_barcodes, csv_barcodes)
                if barcode_error:
                    if "Duplicate" in barcode_error:
                        duplicate_barcodes.append(row)
                    else:
                        invalid_barcodes.append(row)
                    continue
                if barcode:
                    csv_barcodes.add(barcode)

                key = (
                    warehouse_id,
                    zone_id if zone_id is not None else -1,
                    grouping_a,
                    grouping_b or "",
                    grouping_c or "",
                    grouping_d or "",
                )
                if key in seen_keys:
                    duplicate_locations.append(row)
                    continue
                seen_keys.add(key)

                cur.execute(
                    f"""
                    INSERT INTO {VALIDATED_TABLE} (
                        warehouse, warehouseId, zone, zoneId, groupingA, groupingB,
                        groupingC, groupingD, barcode, original_row_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        warehouse_name,
                        warehouse_id,
                        zone_name or None,
                        zone_id,
                        grouping_a,
                        grouping_b or None,
                        grouping_c or None,
                        grouping_d or None,
                        barcode or None,
                        json.dumps(row, ensure_ascii=False),
                    ),
                )
                inserted += 1

        conn.commit()

    finally:
        conn.close()

    log(f"✅ Inserted {inserted} validated location(s).", log_callback)

    _write_problem_csv(account_name, "locations_invalid_warehouses.csv", invalid_warehouses, headers, log_callback)
    _write_problem_csv(account_name, "locations_invalid_zones.csv", invalid_zones, headers, log_callback)
    _write_problem_csv(account_name, "locations_invalid_hierarchy.csv", invalid_hierarchy, headers, log_callback)
    _write_problem_csv(account_name, "locations_invalid_barcodes.csv", invalid_barcodes, headers, log_callback)
    _write_problem_csv(account_name, "locations_duplicate_barcodes.csv", duplicate_barcodes, headers, log_callback)
    _write_problem_csv(account_name, "locations_duplicate_keys.csv", duplicate_locations, headers, log_callback)
    _write_problem_csv(account_name, "locations_invalid_groupings.csv", invalid_groupings, headers, log_callback)

    return inserted


def export_locations_csv(db_path: str, file_path: str, *, log_callback=None) -> int:
    conn = sqlite3.connect(db_path)
    count = 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (REF_LOCATIONS,),
        )
        if cur.fetchone() is None:
            log(
                "⚠️ No location catalogue table found; exporting an empty CSV file.",
                log_callback,
            )
            with open_table(file_path, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(UPDATE_HEADERS)
            return 0

        cur.execute("SELECT COUNT(*) FROM ref_locations")
        total = cur.fetchone()[0] or 0
        log(f"📄 Exporting {total} locations to CSV…", log_callback, level="DEBUG")

        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ref_warehouses'"
        )
        has_ref_warehouses = cur.fetchone() is not None
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (REF_ZONES,),
        )
        has_ref_zones = cur.fetchone() is not None

        warehouse_expression = "rw.name" if has_ref_warehouses else "rl.warehouseId"
        zone_expression = "rz.name" if has_ref_zones else "rl.zoneId"
        warehouse_join = (
            "LEFT JOIN ref_warehouses AS rw ON rw.warehouseId = rl.warehouseId"
            if has_ref_warehouses
            else ""
        )
        zone_join = (
            "LEFT JOIN ref_zones AS rz ON rz.zoneId = rl.zoneId AND rz.warehouseId = rl.warehouseId"
            if has_ref_zones
            else ""
        )

        cur.execute(
            f"""
            SELECT
                rl.locationId,
                {warehouse_expression} AS warehouse,
                {zone_expression} AS zone,
                rl.groupingA,
                rl.groupingB,
                rl.groupingC,
                rl.groupingD,
                rl.barcode
            FROM ref_locations AS rl
            {warehouse_join}
            {zone_join}
            ORDER BY rl.warehouseId, rl.locationId
            """
        )
        with open_table(file_path, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(UPDATE_HEADERS)
            batch_size = 1000
            fetched = cur.fetchmany(batch_size)
            while fetched:
                writer.writerows(fetched)
                count += len(fetched)
                if total:
                    progress = int((count / total) * 100)
                    log(
                        f"📄 Export progress: {count}/{total} ({progress}%)",
                        log_callback,
                        level="DEBUG",
                    )
                fetched = cur.fetchmany(batch_size)
    finally:
        conn.close()
    log(f"✅ Exported {count} locations to CSV.", log_callback, level="DEBUG")
    return count


def validate_location_updates(
    csv_path: str,
    db_path: str,
    account_name: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> dict:
    ensure_account_binding(db_path, account_name)

    conn = sqlite3.connect(db_path)
    inserted = 0
    unmatched_rows: List[dict] = []
    invalid_rows: List[dict] = []
    invalid_zones: List[dict] = []
    invalid_barcodes: List[dict] = []
    duplicate_barcodes: List[dict] = []
    try:
        _ensure_update_table(conn)
        cur = conn.cursor()
        existing_barcodes = {}
        cur.execute(
            f"SELECT barcode, warehouseId, locationId FROM {REF_LOCATIONS} WHERE barcode IS NOT NULL"
        )
        for barcode, warehouse_id, location_id in cur.fetchall():
            if barcode:
                existing_barcodes[str(barcode)] = (int(warehouse_id), int(location_id))

        csv_barcodes = set()
        with open_csv(csv_path) as csv_file:
            reader = csv.DictReader(csv_file)
            for index, row in enumerate(reader, 1):
                if cancel_token and cancel_token.is_set():
                    log(
                        f"🛑 Cancelled during update validation at row {index}. Inserted so far: {inserted}.",
                        log_callback,
                    )
                    break

                location_id = _safe_int(row.get("locationId"))
                warehouse_field = (row.get("warehouse") or row.get("warehouseId") or "").strip()
                warehouse_id = _warehouse_to_id(cur, warehouse_field)
                zone_name = (row.get("zone") or "").strip()
                grouping_a = (row.get("aisle") or "").strip()
                grouping_b = (row.get("bay") or "").strip()
                grouping_c = (row.get("shelf") or "").strip()
                grouping_d = (row.get("bin") or "").strip()
                barcode = (row.get("barcode") or "").strip()

                if location_id is None or warehouse_id is None:
                    invalid_rows.append(row)
                    continue

                cur.execute(
                    f"""
                    SELECT 1 FROM {REF_LOCATIONS}
                    WHERE locationId = ? AND warehouseId = ?
                    """,
                    (location_id, warehouse_id),
                )
                if cur.fetchone() is None:
                    unmatched_rows.append(row)
                    continue

                zone_id = _zone_to_id(cur, warehouse_id, zone_name) if zone_name else None
                if zone_name and zone_id is None:
                    invalid_zones.append(row)
                    continue

                grouping_error = _validate_groupings(
                    grouping_a,
                    grouping_b,
                    grouping_c,
                    grouping_d,
                )
                if grouping_error:
                    invalid_rows.append(row)
                    continue

                barcode_error = _validate_barcode(
                    barcode,
                    existing_barcodes,
                    csv_barcodes,
                    allowed_key=(warehouse_id, location_id),
                )
                if barcode_error:
                    if "Duplicate" in barcode_error:
                        duplicate_barcodes.append(row)
                    else:
                        invalid_barcodes.append(row)
                    continue

                if barcode:
                    csv_barcodes.add(barcode)

                cur.execute(
                    f"""
                    INSERT INTO {UPDATE_TABLE} (
                        locationId, warehouseId, zone, zoneId, groupingA, groupingB,
                        groupingC, groupingD, barcode, original_row_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        location_id,
                        warehouse_id,
                        zone_name or None,
                        zone_id,
                        grouping_a,
                        grouping_b or None,
                        grouping_c or None,
                        grouping_d or None,
                        barcode or None,
                        json.dumps(row, ensure_ascii=False),
                    ),
                )
                inserted += 1

        conn.commit()
    finally:
        conn.close()

    log(f"✅ Inserted {inserted} location update(s).", log_callback)

    _write_unmatched_csv(
        account_name,
        "location_updates_unmatched.csv",
        unmatched_rows,
        log_callback,
    )
    _write_unmatched_csv(
        account_name,
        "location_updates_invalid.csv",
        invalid_rows,
        log_callback,
    )
    _write_unmatched_csv(
        account_name,
        "location_updates_invalid_zones.csv",
        invalid_zones,
        log_callback,
    )
    _write_unmatched_csv(
        account_name,
        "location_updates_invalid_barcodes.csv",
        invalid_barcodes,
        log_callback,
    )
    _write_unmatched_csv(
        account_name,
        "location_updates_duplicate_barcodes.csv",
        duplicate_barcodes,
        log_callback,
    )

    return {
        "inserted": inserted,
        "unmatched": len(unmatched_rows),
        "invalid": len(invalid_rows) + len(invalid_zones) + len(invalid_barcodes),
        "duplicate_barcodes": len(duplicate_barcodes),
    }


def _load_validated_rows(conn: sqlite3.Connection) -> List[dict]:
    cur = conn.cursor()
    ensure_processing_columns(conn, VALIDATED_TABLE)
    cur.execute(
        f"""
        SELECT id, warehouseId, COALESCE(zoneId, -1), groupingA, groupingB, groupingC, groupingD, barcode
        FROM {VALIDATED_TABLE}
        WHERE {unprocessed_where_clause()}
        """
    )
    rows = []
    for row_id, wid, zid, grouping_a, grouping_b, grouping_c, grouping_d, barcode in cur.fetchall():
        rows.append(
            {
                "id": row_id,
                "warehouseId": wid,
                "zoneId": None if zid == -1 else zid,
                "groupingA": grouping_a,
                "groupingB": grouping_b,
                "groupingC": grouping_c,
                "groupingD": grouping_d,
                "barcode": barcode,
            }
        )
    return rows


def _location_exists(conn: sqlite3.Connection, payload: dict, warehouse_id: int) -> Optional[int]:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id FROM {REF_LOCATIONS}
        WHERE warehouseId = ?
          AND IFNULL(zoneId,-1) = IFNULL(?, -1)
          AND groupingA = ?
          AND IFNULL(groupingB,'') = IFNULL(?, '')
          AND IFNULL(groupingC,'') = IFNULL(?, '')
          AND IFNULL(groupingD,'') = IFNULL(?, '')
        """,
        (
            warehouse_id,
            payload.get("zoneId"),
            payload.get("groupingA"),
            payload.get("groupingB"),
            payload.get("groupingC"),
            payload.get("groupingD"),
        ),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _upsert_location_ref(
    conn: sqlite3.Connection, payload: dict, warehouse_id: int, location_id: Optional[int]
) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT OR IGNORE INTO {REF_LOCATIONS}
        (warehouseId, zoneId, groupingA, groupingB, groupingC, groupingD, barcode)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            warehouse_id,
            payload.get("zoneId"),
            payload.get("groupingA"),
            payload.get("groupingB"),
            payload.get("groupingC"),
            payload.get("groupingD"),
            payload.get("barcode"),
        ),
    )
    conn.commit()


def _update_location_ref(
    conn: sqlite3.Connection,
    warehouse_id: int,
    location_id: int,
    payload: dict,
) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        UPDATE {REF_LOCATIONS}
        SET zoneId = ?, groupingA = ?, groupingB = ?, groupingC = ?, groupingD = ?, barcode = ?
        WHERE warehouseId = ? AND locationId = ?
        """,
        (
            payload.get("zoneId"),
            payload.get("groupingA"),
            payload.get("groupingB"),
            payload.get("groupingC"),
            payload.get("groupingD"),
            payload.get("barcode"),
            warehouse_id,
            location_id,
        ),
    )
    conn.commit()


def _build_payload(row: dict) -> dict:
    payload = {"groupingA": row["groupingA"]}
    if row.get("zoneId") is not None:
        payload["zoneId"] = int(row["zoneId"])
    if row.get("groupingB"):
        payload["groupingB"] = row["groupingB"]
    if row.get("groupingC"):
        payload["groupingC"] = row["groupingC"]
    if row.get("groupingD"):
        payload["groupingD"] = row["groupingD"]
    if row.get("barcode"):
        payload["barcode"] = row["barcode"]
    return payload


def _parse_int_header(headers: requests.structures.CaseInsensitiveDict, key: str, default: int = 0) -> int:
    try:
        return int(headers.get(key, default))
    except (TypeError, ValueError):
        return default


def _put_location(
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
            response = requests.put(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("PUT", url, payload, response, log_callback)
            status = response.status_code
            log(f"PUT {url} attempt {attempt} → {status}", log_callback)

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


def _post_location(
    url: str,
    headers: dict,
    payload: dict,
    *,
    log_callback=None,
    cancel_token=None,
) -> Tuple[bool, Optional[int], int, int]:
    """Post a location to Brightpearl, returning success and throttling hints."""

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
                    created_id = int(data["response"]["locationId"])
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


def sync_locations(
    account_name: str,
    db_path: str,
    *,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Sync validated locations to Brightpearl."""

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
        log(f"🔄 Syncing {total} location(s)…", log_callback)

        headers = {**credentials.headers, "Content-Type": "application/json"}

        for idx, row in enumerate(rows, 1):
            if cancel_token and cancel_token.is_set():
                log(
                    f"🛑 Sync cancelled after processing {idx - 1} of {total} rows.",
                    log_callback,
                )
                break

            warehouse_id = row["warehouseId"]
            payload = _build_payload(row)

            existing_id = _location_exists(conn, payload, warehouse_id)
            if existing_id:
                mark_rows_processed(conn, VALIDATED_TABLE, row["id"], f"Location already exists {existing_id}")
                conn.commit()
                log(
                    f"⏭️ Skipping existing location (warehouse {warehouse_id}) → {existing_id} :: {payload}",
                    log_callback,
                )
                progress = int((idx / max(total, 1)) * 100)
                log(f"PROGRESS:{progress}", log_callback)
                continue

            url = (
                f"https://{credentials.region}.brightpearlconnect.com/public-api/"
                f"{account_name}/warehouse-service/warehouse/{warehouse_id}/location/"
            )

            ok, created_id, requests_remaining, throttle_ms = _post_location(
                url,
                headers,
                payload,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )

            if ok:
                mark_rows_processed(conn, VALIDATED_TABLE, row["id"], f"Created location {created_id or payload}")
                conn.commit()
                _upsert_location_ref(conn, payload, warehouse_id, created_id)
                created_count += 1
                log(
                    f"✅ Location created (WH {warehouse_id}) → {created_id or 'OK'} :: {payload}",
                    log_callback,
                )
            else:
                log(
                    f"❌ Failed to create location (WH {warehouse_id}) :: {payload}",
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
        log("✅ Locations sync complete.", log_callback)

    return created_count


def sync_location_updates(
    account_name: str,
    db_path: str,
    *,
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
    updated_count = 0
    try:
        cur = conn.cursor()
        ensure_processing_columns(conn, UPDATE_TABLE)
        cur.execute(
            f"""
            SELECT id, locationId, warehouseId, zoneId, groupingA, groupingB, groupingC, groupingD, barcode
            FROM {UPDATE_TABLE}
            WHERE {unprocessed_where_clause()}
            """
        )
        rows = cur.fetchall()
        total = len(rows)
        if total == 0:
            log("⚠️ No location updates to sync.", log_callback)
            return 0

        log(f"🔄 Syncing {total} location update(s)…", log_callback)

        headers = {**credentials.headers, "Content-Type": "application/json"}

        for idx, row in enumerate(rows, 1):
            if cancel_token and cancel_token.is_set():
                log(
                    f"🛑 Sync cancelled after processing {idx - 1} of {total} rows.",
                    log_callback,
                )
                break

            row_id = int(row["id"])
            location_id = int(row["locationId"])
            warehouse_id = int(row["warehouseId"])
            payload = _build_payload(dict(row))

            cur.execute(
                f"""
                SELECT zoneId, groupingA, groupingB, groupingC, groupingD, barcode
                FROM {REF_LOCATIONS}
                WHERE locationId = ? AND warehouseId = ?
                """,
                (location_id, warehouse_id),
            )
            existing = cur.fetchone()
            if not existing:
                log(
                    f"⚠️ Missing reference for location {location_id} (WH {warehouse_id}); skipping.",
                    log_callback,
                )
                continue

            compare_fields = [
                ("zoneId", existing["zoneId"], row["zoneId"]),
                ("groupingA", existing["groupingA"], row["groupingA"]),
                ("groupingB", existing["groupingB"], row["groupingB"]),
                ("groupingC", existing["groupingC"], row["groupingC"]),
                ("groupingD", existing["groupingD"], row["groupingD"]),
                ("barcode", existing["barcode"], row["barcode"]),
            ]
            if all((current or None) == (updated or None) for _, current, updated in compare_fields):
                mark_rows_processed(conn, UPDATE_TABLE, row_id, f"No changes for location {location_id}")
                conn.commit()
                log(
                    f"⏭️ No changes for location {location_id} (WH {warehouse_id}); skipping.",
                    log_callback,
                )
                progress = int((idx / max(total, 1)) * 100)
                log(f"PROGRESS:{progress}", log_callback)
                continue

            url = (
                f"https://{credentials.region}.brightpearlconnect.com/public-api/"
                f"{account_name}/warehouse-service/warehouse/{warehouse_id}/location/{location_id}"
            )

            ok, requests_remaining, throttle_ms = _put_location(
                url,
                headers,
                payload,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )

            if ok:
                mark_rows_processed(conn, UPDATE_TABLE, row_id, f"Updated location {location_id}")
                conn.commit()
                _update_location_ref(conn, warehouse_id, location_id, payload)
                updated_count += 1
                log(
                    f"✅ Location updated (WH {warehouse_id}, ID {location_id}) :: {payload}",
                    log_callback,
                )
            else:
                log(
                    f"❌ Failed to update location (WH {warehouse_id}, ID {location_id}) :: {payload}",
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
        log("🟡 Location update sync exited due to cancellation.", log_callback)
    else:
        log("✅ Location update sync complete.", log_callback)

    return updated_count


__all__ = [
    "TEMPLATE_HEADERS",
    "VALIDATED_TABLE",
    "UPDATE_TABLE",
    "UPDATE_HEADERS",
    "export_locations_csv",
    "validate_location_updates",
    "sync_location_updates",
    "update_location_catalogue",
    "validate_locations",
    "sync_locations",
]
