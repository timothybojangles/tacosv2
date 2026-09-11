import csv
from csv_safety import open_csv
import sqlite3
import os
import re
import sys

from brightpearl.common import processing_column_definitions, connect_sqlite, ensure_account_binding, log_sync
from brightpearl.settings import get_settings
from row_validation import product_field_length_errors, row_with_validation_error

VALIDATED_TABLE = "validated_inventory"
REF_LOCATIONS_TABLE = "ref_locations"

def _get_location_value(row):
    for key in ("locationName", "location", "locationId"):
        value = row.get(key)
        if value is not None:
            return str(value).strip()
    return ""

def _lookup_location_id(cur, warehouse_id, location_value):
    if not location_value:
        return None

    location_value = location_value.strip()
    if not location_value:
        return None

    if location_value.isdigit():
        return int(location_value)

    parts = [part.strip() for part in location_value.split(".")]
    if len(parts) > 4 or any(part == "" for part in parts):
        return None

    while len(parts) < 4:
        parts.append(None)

    grouping_a, grouping_b, grouping_c, grouping_d = (
        part.upper() if part is not None else None for part in parts
    )
    cur.execute(
        f"""
        SELECT locationId
        FROM {REF_LOCATIONS_TABLE}
        WHERE warehouseId = ?
          AND UPPER(groupingA) = ?
          AND UPPER(COALESCE(groupingB, '')) = ?
          AND UPPER(COALESCE(groupingC, '')) = ?
          AND UPPER(COALESCE(groupingD, '')) = ?
        """,
        (
            warehouse_id,
            grouping_a,
            grouping_b or "",
            grouping_c or "",
            grouping_d or "",
        ),
    )
    row = cur.fetchone()
    return row[0] if row else None

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

def log(msg, log_callback=None):
    log_sync(msg, log_callback)

def _ensure_ref_locations_table(cur):
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REF_LOCATIONS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

def validate_and_enrich_inventory(csv_path, db_path, account_name, region=None, log_callback=None, cancel_token=None):
    # ... keep your existing preamble ...

    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    _ensure_ref_locations_table(cur)
    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT, quantity REAL, locationId TEXT, costprice REAL,
            warehouseId TEXT, productId INTEGER, stockTracked BOOLEAN{processing_column_definitions()}
        )
    """)

    unmatched = []
    non_stock_tracked = []
    unmatched_locations = []
    missing_required_row = []
    inserted_count = 0

    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        for idx, row in enumerate(reader, 1):
            if cancel_token and cancel_token.is_set():
                log(f"🛑 Cancel at row {idx}. Inserted so far: {inserted_count}.", log_callback)
                break

            sku = row["sku"].strip()
            warehouse_field = (row.get("warehouseId") or row.get("warehouseName") or row.get("warehouse") or "").strip()
            quantity = row.get("quantity", "").strip()
            costprice = row.get("costprice", "").strip()
            location_value = _get_location_value(row)

            length_errors = product_field_length_errors(sku)
            if length_errors:
                missing_required_row.append(row_with_validation_error(row, length_errors))
                continue

            if not sku or not warehouse_field:
                continue

            cur.execute("SELECT productId, stockTracked FROM product_catalogue WHERE SKU = ?", (sku,))
            result = cur.fetchone()

            if not result:
                unmatched.append(row); continue

            productId, stockTracked = result
            if not stockTracked:
                non_stock_tracked.append(row); continue

            warehouse_id_int = _warehouse_to_id(cur, warehouse_field)
            if warehouse_id_int is None:
                unmatched_locations.append(row); continue

            locationId = _lookup_location_id(cur, warehouse_id_int, location_value)
            if locationId is None:
                unmatched_locations.append(row); continue

            cur.execute(f"""
                INSERT INTO {VALIDATED_TABLE} (sku, quantity, locationId, costprice, warehouseId, productId, stockTracked)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sku, quantity, locationId, costprice, str(warehouse_id_int), productId, stockTracked))
            inserted_count += 1

    conn.commit(); conn.close()

    log("✅ Inventory validated and enriched.", log_callback)
    log(f"✔️ {inserted_count} validated items inserted into {VALIDATED_TABLE}.", log_callback)

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)

    # Write rows that exceed Brightpearl's product identifier limits.
    if missing_required_row:
        missing_required_file = os.path.join(
            unmatched_dir, f"{account_name}_inventory_missing_required_row.csv"
        )
        with open(missing_required_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[*(reader.fieldnames or []), "validation_error"])
            writer.writeheader()
            writer.writerows(missing_required_row)
        log(
            f"⚠️ {len(missing_required_row)} invalid rows written to {missing_required_file}",
            log_callback,
        )

    # Write unmatched SKUs
    if unmatched:
        unmatched_file = os.path.join(unmatched_dir, f"{account_name}_unmatched_skus.csv")
        with open(unmatched_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=reader.fieldnames)
            writer.writeheader()
            writer.writerows(unmatched)
        log(f"⚠️ {len(unmatched)} unmatched SKUs written to {unmatched_file}", log_callback)

    # Write non-stock-tracked SKUs
    if non_stock_tracked:
        non_tracked_file = os.path.join(unmatched_dir, f"{account_name}_non_stock_tracked.csv")
        with open(non_tracked_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=reader.fieldnames)
            writer.writeheader()
            writer.writerows(non_stock_tracked)
        log(f"⚠️ {len(non_stock_tracked)} non-stock-tracked SKUs written to {non_tracked_file}", log_callback)

    # Write unmatched locations
    if unmatched_locations:
        unmatched_locations_file = os.path.join(unmatched_dir, f"{account_name}_unmatched_locations.csv")
        with open(unmatched_locations_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=reader.fieldnames)
            writer.writeheader()
            writer.writerows(unmatched_locations)
        log(
            f"⚠️ {len(unmatched_locations)} unmatched locations written to {unmatched_locations_file}",
            log_callback,
        )

    return inserted_count   
# Optional CLI usage (without GUI)
if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python validator.py <account_name> <db_path> <csv_path>")
        sys.exit(1)

    account_name = sys.argv[1]
    db_path = sys.argv[2]
    csv_path = sys.argv[3]

    validate_and_enrich_inventory(csv_path, db_path, account_name)
