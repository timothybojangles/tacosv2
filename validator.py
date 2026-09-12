import csv
from csv_safety import open_csv
import sqlite3
import os
import re
import sys
from decimal import Decimal, InvalidOperation

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
        cur.execute(
            f"SELECT locationId FROM {REF_LOCATIONS_TABLE} WHERE warehouseId = ? AND locationId = ?",
            (warehouse_id, int(location_value)),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None

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


def _parse_decimal(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    return float(parsed)

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
    rejected_rows = []
    inserted_count = 0

    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        for idx, row in enumerate(reader, 1):
            if cancel_token and cancel_token.is_set():
                log(f"🛑 Cancel at row {idx}. Inserted so far: {inserted_count}.", log_callback)
                break

            sku = row.get("sku", "").strip()
            warehouse_field = (row.get("warehouseId") or row.get("warehouseName") or row.get("warehouse") or "").strip()
            quantity = row.get("quantity", "").strip()
            costprice = row.get("costprice", "").strip()
            location_value = _get_location_value(row)

            required_values = {
                "sku": sku,
                "quantity": quantity,
                "locationName": location_value,
                "costprice": costprice,
                "warehouseId": warehouse_field,
            }
            categories = []
            validation_errors = []

            def reject(category, message):
                if category not in categories:
                    categories.append(category)
                if message not in validation_errors:
                    validation_errors.append(message)

            missing_fields = [name for name, value in required_values.items() if not value]
            if missing_fields:
                reject("missing_required", f"missing required fields: {', '.join(missing_fields)}")

            length_errors = product_field_length_errors(sku)
            for error in length_errors:
                reject("missing_required", error)

            parsed_quantity = _parse_decimal(quantity)
            parsed_costprice = _parse_decimal(costprice)
            if quantity and parsed_quantity is None:
                reject("missing_required", "quantity must be a finite number")
            if costprice and parsed_costprice is None:
                reject("missing_required", "costprice must be a finite number")

            result = None
            if sku:
                cur.execute("SELECT productId, stockTracked FROM product_catalogue WHERE SKU = ?", (sku,))
                result = cur.fetchone()
                if not result:
                    reject("unmatched_sku", "SKU was not found in the synced product catalogue")
                elif not result[1]:
                    reject("non_stock_tracked", "Product is not stock tracked")

            warehouse_id_int = _warehouse_to_id(cur, warehouse_field) if warehouse_field else None
            if warehouse_field and warehouse_id_int is None:
                reject("unmatched_location", "Warehouse was not found in synced references")

            locationId = None
            if warehouse_id_int is not None and location_value:
                locationId = _lookup_location_id(cur, warehouse_id_int, location_value)
                if locationId is None:
                    reject("unmatched_location", "Location was not found in the selected warehouse")

            if categories:
                if "missing_required" in categories:
                    missing_required_row.append(row_with_validation_error(row, validation_errors))
                if "unmatched_sku" in categories:
                    unmatched.append(row)
                if "non_stock_tracked" in categories:
                    non_stock_tracked.append(row)
                if "unmatched_location" in categories:
                    unmatched_locations.append(row)
                rejected_rows.append({
                    "source_row": idx,
                    "validation_categories": "; ".join(categories),
                    "validation_error": "; ".join(validation_errors),
                    **row,
                })
                continue

            productId, stockTracked = result

            cur.execute(f"""
                INSERT INTO {VALIDATED_TABLE} (sku, quantity, locationId, costprice, warehouseId, productId, stockTracked)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sku, parsed_quantity, locationId, parsed_costprice, str(warehouse_id_int), productId, stockTracked))
            inserted_count += 1

    conn.commit(); conn.close()

    log("✅ Inventory validated and enriched.", log_callback)
    log(f"✔️ {inserted_count} validated items inserted into {VALIDATED_TABLE}.", log_callback)

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)

    if rejected_rows:
        rejected_file = os.path.join(unmatched_dir, f"{account_name}_inventory_rejected.csv")
        with open(rejected_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "source_row",
                    "validation_categories",
                    "validation_error",
                    *(reader.fieldnames or []),
                ],
            )
            writer.writeheader()
            writer.writerows(rejected_rows)

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
