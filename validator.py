import csv
import os
import re
import sys

from csv_safety import open_csv

from brightpearl.common import processing_column_definitions, connect_sqlite, log_sync
from brightpearl.settings import get_settings
from row_validation import product_field_length_errors, row_with_validation_error


VALIDATED_TABLE = "validated_inventory"
REF_LOCATIONS_TABLE = "ref_locations"
PROGRESS_INTERVAL = 500
HEARTBEAT_INTERVAL = 5_000
EXCEPTION_FLUSH_INTERVAL = 500


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


def _count_csv_rows(csv_path):
    """Count data rows without retaining them so determinate progress is possible."""
    with open_csv(csv_path) as csvfile:
        return sum(1 for _ in csv.DictReader(csvfile))


class _ExceptionCsvOutputs:
    """Lazily stream validation failures to category-specific CSV files."""

    _FILE_SUFFIXES = {
        "missing_required": "inventory_missing_required_row.csv",
        "unmatched": "unmatched_skus.csv",
        "non_stock_tracked": "non_stock_tracked.csv",
        "unmatched_locations": "unmatched_locations.csv",
    }

    def __init__(self, output_dir, account_name, fieldnames):
        self.output_dir = output_dir
        self.account_name = account_name
        self.fieldnames = list(fieldnames or [])
        self._handles = {}
        self._writers = {}
        self._row_counts = {}
        self.paths = {}
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            self.paths = {
                category: os.path.join(output_dir, f"{account_name}_{suffix}")
                for category, suffix in self._FILE_SUFFIXES.items()
            }
            for path in self.paths.values():
                if os.path.exists(path):
                    os.remove(path)

    def write(self, category, row):
        if not self.output_dir:
            return
        writer = self._writers.get(category)
        if writer is None:
            path = self.paths[category]
            handle = open(path, "w", newline="", encoding="utf-8")
            fieldnames = list(self.fieldnames)
            if category == "missing_required" and "validation_error" not in fieldnames:
                fieldnames.append("validation_error")
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            self._handles[category] = handle
            self._writers[category] = writer
            self._row_counts[category] = 0
        writer.writerow(row)
        self._row_counts[category] += 1
        if self._row_counts[category] % EXCEPTION_FLUSH_INTERVAL == 0:
            self._handles[category].flush()

    def close(self):
        for handle in self._handles.values():
            handle.close()


def _report_progress(completed, total, counts, log_callback, *, force=False):
    if total and (force or completed % PROGRESS_INTERVAL == 0):
        percent = min(100.0, (completed / total) * 100)
        log(f"PROGRESS:{percent:.1f}", log_callback)

    if completed and completed % HEARTBEAT_INTERVAL == 0:
        log(
            f"Validated {completed:,} / {total:,} rows — "
            f"{counts['valid']:,} valid, {counts['unmatched']:,} unmatched SKU, "
            f"{counts['non_stock_tracked']:,} non-stock-tracked, "
            f"{counts['unmatched_locations']:,} location errors, "
            f"{counts['missing_required']:,} invalid required fields.",
            log_callback,
        )


def validate_and_enrich_inventory(csv_path, db_path, account_name, region=None, log_callback=None, cancel_token=None):
    total_rows = _count_csv_rows(csv_path)
    log(f"Validating {total_rows:,} inventory rows.", log_callback)
    log("PROGRESS:0", log_callback)

    conn = connect_sqlite(db_path)
    cur = conn.cursor()
    _ensure_ref_locations_table(cur)
    log("Preparing the product catalogue SKU index.", log_callback)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_catalogue_sku ON product_catalogue(SKU)"
    )
    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT, quantity REAL, locationId TEXT, costprice REAL,
            warehouseId TEXT, productId INTEGER, stockTracked BOOLEAN{processing_column_definitions()}
        )
    """)

    counts = {
        "valid": 0,
        "unmatched": 0,
        "non_stock_tracked": 0,
        "unmatched_locations": 0,
        "missing_required": 0,
    }
    processed_count = 0
    cancelled = False
    exception_outputs = None

    try:
        with open_csv(csv_path) as csvfile:
            reader = csv.DictReader(csvfile)
            exception_outputs = _ExceptionCsvOutputs(
                get_settings().unmatched_output_dir,
                account_name,
                reader.fieldnames,
            )

            for idx, row in enumerate(reader, 1):
                if cancel_token and cancel_token.is_set():
                    cancelled = True
                    log(
                        f"🛑 Cancel at row {idx}. Inserted so far: {counts['valid']}.",
                        log_callback,
                    )
                    break

                try:
                    sku = (row.get("sku") or "").strip()
                    warehouse_field = (
                        row.get("warehouseId")
                        or row.get("warehouseName")
                        or row.get("warehouse")
                        or ""
                    ).strip()
                    quantity = (row.get("quantity") or "").strip()
                    costprice = (row.get("costprice") or "").strip()
                    location_value = _get_location_value(row)

                    validation_errors = product_field_length_errors(sku)
                    if not sku:
                        validation_errors.append("SKU is required")
                    if not warehouse_field:
                        validation_errors.append("Warehouse is required")
                    if validation_errors:
                        exception_outputs.write(
                            "missing_required",
                            row_with_validation_error(row, validation_errors),
                        )
                        counts["missing_required"] += 1
                        continue

                    cur.execute(
                        "SELECT productId, stockTracked FROM product_catalogue WHERE SKU = ?",
                        (sku,),
                    )
                    result = cur.fetchone()
                    if not result:
                        exception_outputs.write("unmatched", row)
                        counts["unmatched"] += 1
                        continue

                    product_id, stock_tracked = result
                    if not stock_tracked:
                        exception_outputs.write("non_stock_tracked", row)
                        counts["non_stock_tracked"] += 1
                        continue

                    warehouse_id_int = _warehouse_to_id(cur, warehouse_field)
                    if warehouse_id_int is None:
                        exception_outputs.write("unmatched_locations", row)
                        counts["unmatched_locations"] += 1
                        continue

                    location_id = _lookup_location_id(cur, warehouse_id_int, location_value)
                    if location_id is None:
                        exception_outputs.write("unmatched_locations", row)
                        counts["unmatched_locations"] += 1
                        continue

                    cur.execute(f"""
                        INSERT INTO {VALIDATED_TABLE} (
                            sku, quantity, locationId, costprice,
                            warehouseId, productId, stockTracked
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        sku,
                        quantity,
                        location_id,
                        costprice,
                        str(warehouse_id_int),
                        product_id,
                        stock_tracked,
                    ))
                    counts["valid"] += 1
                finally:
                    processed_count = idx
                    _report_progress(
                        processed_count,
                        total_rows,
                        counts,
                        log_callback,
                    )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if exception_outputs is not None:
            exception_outputs.close()
        conn.close()

    _report_progress(processed_count, total_rows, counts, log_callback, force=True)
    if not cancelled and total_rows == 0:
        log("PROGRESS:100.0", log_callback)

    log("✅ Inventory validated and enriched.", log_callback)
    log(
        f"✔️ {counts['valid']} validated items inserted into {VALIDATED_TABLE}.",
        log_callback,
    )

    labels = {
        "missing_required": "invalid rows",
        "unmatched": "unmatched SKUs",
        "non_stock_tracked": "non-stock-tracked SKUs",
        "unmatched_locations": "unmatched locations",
    }
    if exception_outputs is not None:
        for category, label in labels.items():
            if counts[category] and category in exception_outputs.paths:
                log(
                    f"⚠️ {counts[category]} {label} written to "
                    f"{exception_outputs.paths[category]}",
                    log_callback,
                )

    return counts["valid"]


# Optional CLI usage (without GUI)
if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python validator.py <account_name> <db_path> <csv_path>")
        sys.exit(1)

    account_name = sys.argv[1]
    db_path = sys.argv[2]
    csv_path = sys.argv[3]

    validate_and_enrich_inventory(csv_path, db_path, account_name)
