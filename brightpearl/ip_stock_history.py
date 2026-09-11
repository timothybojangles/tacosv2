"""Helpers for Export > IP Stock History module."""

from __future__ import annotations

import csv
from csv_safety import open_csv, open_table
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

AUDIT_TRAIL_HEADERS = [
    "Product ID",
    "SKU",
    "Product Name",
    "Options",
    "Quantity",
    "Price",
    "Reference",
    "Warehouse",
    "Date",
    "Movement ID",
]


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _ensure_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ip_stock_history_audit_trail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT,
            sku TEXT NOT NULL,
            product_name TEXT,
            options TEXT,
            quantity INTEGER NOT NULL,
            price TEXT,
            reference TEXT,
            warehouse TEXT NOT NULL,
            date_text TEXT NOT NULL,
            date_iso TEXT NOT NULL,
            movement_id TEXT
        )
        """
    )


def _parse_date(date_text: str) -> tuple[str, str]:
    raw = (date_text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return raw, dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {date_text!r}")


def import_ip_stock_history_audit_csv(db_path: str, csv_path: str) -> int:
    with open_csv(csv_path) as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        incoming = {_normalize_header(name): name for name in fieldnames}
        required = [_normalize_header(name) for name in AUDIT_TRAIL_HEADERS]
        missing = [AUDIT_TRAIL_HEADERS[i] for i, key in enumerate(required) if key not in incoming]
        if missing:
            raise ValueError(f"CSV is missing required column(s): {', '.join(missing)}")

        rows: list[tuple[str, str, str, str, int, str, str, str, str, str, str]] = []
        for row in reader:
            quantity_text = (row[incoming[_normalize_header("Quantity")]] or "").strip()
            if not quantity_text:
                continue
            sku = (row[incoming[_normalize_header("SKU")]] or "").strip()
            if not sku:
                continue
            quantity = int(float(quantity_text))
            date_text, date_iso = _parse_date(row[incoming[_normalize_header("Date")]])
            rows.append(
                (
                    (row[incoming[_normalize_header("Product ID")]] or "").strip(),
                    sku,
                    (row[incoming[_normalize_header("Product Name")]] or "").strip(),
                    (row[incoming[_normalize_header("Options")]] or "").strip(),
                    quantity,
                    (row[incoming[_normalize_header("Price")]] or "").strip(),
                    (row[incoming[_normalize_header("Reference")]] or "").strip(),
                    (row[incoming[_normalize_header("Warehouse")]] or "").strip(),
                    date_text,
                    date_iso,
                    (row[incoming[_normalize_header("Movement ID")]] or "").strip(),
                )
            )

    with sqlite3.connect(db_path) as conn:
        _ensure_audit_table(conn)
        conn.execute("DELETE FROM ip_stock_history_audit_trail")
        conn.executemany(
            """
            INSERT INTO ip_stock_history_audit_trail (
                product_id, sku, product_name, options, quantity,
                price, reference, warehouse, date_text, date_iso, movement_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def export_ip_stock_history_by_warehouse(
    db_path: str, output_dir: str, *, file_format: str = "csv"
) -> list[str]:
    """Export one stock-history spreadsheet per warehouse as CSV or XLSX."""
    file_format = file_format.lower().lstrip(".")
    if file_format not in {"csv", "xlsx"}:
        raise ValueError("file_format must be 'csv' or 'xlsx'")
    with sqlite3.connect(db_path) as conn:
        _ensure_audit_table(conn)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT warehouse, sku, date_iso, SUM(quantity) AS movement_qty
            FROM ip_stock_history_audit_trail
            WHERE TRIM(COALESCE(sku, '')) <> ''
            GROUP BY warehouse, sku, date_iso
            ORDER BY warehouse, sku, date_iso
            """
        )
        rows = cur.fetchall()

    grouped: dict[str, dict[str, list[tuple[str, int]]]] = defaultdict(lambda: defaultdict(list))
    for warehouse, sku, date_iso, movement_qty in rows:
        grouped[warehouse][sku].append((date_iso, int(movement_qty or 0)))

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    for warehouse, sku_map in grouped.items():
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", warehouse).strip("_") or "warehouse"
        file_path = output_path / f"ip_stock_history_{safe_name}.{file_format}"
        with open_table(file_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["SKU", "Date", "Stock"])
            for sku, date_rows in sorted(sku_map.items()):
                running = 0
                for date_text, movement_qty in date_rows:
                    running += movement_qty
                    writer.writerow([sku, date_text, running])
        created.append(str(file_path))

    return created
