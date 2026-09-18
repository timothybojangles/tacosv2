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
PROGRESS_INTERVAL = 500


class _ExceptionOutputs:
    def __init__(self, directory, account_name, fieldnames):
        self.directory = directory
        self.account_name = account_name
        self.fieldnames = list(fieldnames or [])
        self.handles = {}
        self.writers = {}
        if directory:
            os.makedirs(directory, exist_ok=True)
            for suffix in ("inventory_rejected", "inventory_missing_required_row", "unmatched_skus", "non_stock_tracked", "unmatched_locations"):
                path = os.path.join(directory, f"{account_name}_{suffix}.csv")
                if os.path.exists(path):
                    os.remove(path)

    def write(self, suffix, row):
        if not self.directory:
            return
        if suffix not in self.writers:
            path = os.path.join(self.directory, f"{self.account_name}_{suffix}.csv")
            handle = open(path, "w", newline="", encoding="utf-8")
            fields = self.fieldnames[:]
            if suffix == "inventory_rejected":
                fields = ["source_row", "validation_categories", "validation_error", *fields]
            elif suffix == "inventory_missing_required_row":
                fields.append("validation_error")
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            self.handles[suffix] = handle
            self.writers[suffix] = writer
        self.writers[suffix].writerow(row)

    def close(self):
        for handle in self.handles.values():
            handle.close()

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
    text = str(value if value is not None else "").strip()
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

def validate_and_enrich_inventory(csv_path, db_path, account_name, region=None, log_callback=None,
                                  cancel_token=None, allow_zero_blanks=False, price_list_id=None):
    with open_csv(csv_path) as csvfile:
        total_rows = sum(1 for _ in csv.DictReader(csvfile))
    log(f"Validating {total_rows:,} inventory rows.", log_callback)
    log("PROGRESS:0", log_callback)

    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    _ensure_ref_locations_table(cur)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_catalogue_sku ON product_catalogue(SKU)")
    if price_list_id is not None:
        cur.execute("SELECT 1 FROM ref_price_lists WHERE priceListId = ?", (price_list_id,))
        if not cur.fetchone():
            conn.close()
            raise ValueError("Selected price list is not present in synced references")
    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT, quantity REAL, locationId TEXT, costprice REAL,
            warehouseId TEXT, productId INTEGER, stockTracked BOOLEAN{processing_column_definitions()}
        )
    """)

    inserted_count = 0
    rejected_count = 0

    try:
        with open_csv(csv_path) as csvfile:
            reader = csv.DictReader(csvfile)
            outputs = _ExceptionOutputs(get_settings().unmatched_output_dir, account_name, reader.fieldnames)
            for idx, row in enumerate(reader, 1):
                if cancel_token and cancel_token.is_set():
                    log(f"Cancelled at row {idx}. Inserted so far: {inserted_count}.", log_callback)
                    break

                sku = (row.get("sku") or "").strip()
                warehouse_field = (row.get("warehouseId") or row.get("warehouseName") or row.get("warehouse") or "").strip()
                quantity = (row.get("quantity") or "").strip()
                costprice = (row.get("costprice") or "").strip()
                location_value = _get_location_value(row)

                required_values = {
                    "sku": sku,
                    "locationName": location_value,
                    "warehouseId": warehouse_field,
                }
                if not allow_zero_blanks:
                    required_values["quantity"] = quantity
                    if price_list_id is None:
                        required_values["costprice"] = costprice
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
                if allow_zero_blanks and not quantity:
                    parsed_quantity = 0.0
                if allow_zero_blanks and not costprice and price_list_id is None:
                    parsed_costprice = 0.0
                if quantity and parsed_quantity is None:
                    reject("missing_required", "quantity must be a finite number")
                if price_list_id is None and costprice and parsed_costprice is None:
                    reject("missing_required", "costprice must be a finite number")

                if not allow_zero_blanks and parsed_quantity == 0.0:
                    reject("missing_required", "quantity must be non-zero")
                if price_list_id is None and not allow_zero_blanks and parsed_costprice == 0.0:
                    reject("missing_required", "costprice must be non-zero")

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

                if result and price_list_id is not None:
                    cur.execute(
                        "SELECT value FROM ref_price_list_values WHERE productId = ? AND priceListId = ?",
                        (result[0], price_list_id),
                    )
                    price_row = cur.fetchone()
                    price_value = price_row[0] if price_row else None
                    if price_value is None or str(price_value).strip() == "":
                        if allow_zero_blanks:
                            parsed_costprice = 0.0
                        else:
                            parsed_costprice = None
                            reject("missing_required", "No value in the selected price list")
                    else:
                        parsed_costprice = _parse_decimal(price_value)
                        if parsed_costprice is None:
                            reject("missing_required", "Price list value must be a finite number")
                        elif not allow_zero_blanks and parsed_costprice == 0.0:
                            reject("missing_required", "Price list value must be non-zero")

                if categories:
                    if "missing_required" in categories:
                        outputs.write("inventory_missing_required_row", row_with_validation_error(row, validation_errors))
                    if "unmatched_sku" in categories:
                        outputs.write("unmatched_skus", row)
                    if "non_stock_tracked" in categories:
                        outputs.write("non_stock_tracked", row)
                    if "unmatched_location" in categories:
                        outputs.write("unmatched_locations", row)
                    outputs.write("inventory_rejected", {
                        "source_row": idx,
                        "validation_categories": "; ".join(categories),
                        "validation_error": "; ".join(validation_errors),
                        **row,
                    })
                    rejected_count += 1
                else:
                    productId, stockTracked = result
                    cur.execute(f"""
                        INSERT INTO {VALIDATED_TABLE} (sku, quantity, locationId, costprice, warehouseId, productId, stockTracked)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (sku, parsed_quantity, locationId, parsed_costprice, str(warehouse_id_int), productId, stockTracked))
                    inserted_count += 1

                if idx % PROGRESS_INTERVAL == 0:
                    conn.commit()
                    log(f"PROGRESS:{idx / max(total_rows, 1) * 100:.1f}", log_callback)
                    if idx % 5000 == 0:
                        log(f"Validated {idx:,}/{total_rows:,}: {inserted_count:,} accepted, {rejected_count:,} rejected.", log_callback)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if "outputs" in locals():
            outputs.close()
        conn.close()

    log("PROGRESS:100.0", log_callback)

    log("✅ Inventory validated and enriched.", log_callback)
    log(f"✔️ {inserted_count} validated items inserted into {VALIDATED_TABLE}.", log_callback)

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
