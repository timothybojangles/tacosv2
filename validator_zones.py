"""CLI helper for validating warehouse zones CSV files."""
from __future__ import annotations

import sys

from brightpearl.warehouse_zones import validate_zones


def main(account_name: str, db_path: str, csv_path: str) -> int:
    """Validate ``csv_path`` for ``account_name`` using ``db_path``."""

    return validate_zones(csv_path, db_path, account_name)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python validator_zones.py <account_name> <db_path> <csv_path>")
        sys.exit(1)

    account = sys.argv[1]
    database = sys.argv[2]
    csv_file = sys.argv[3]
    inserted_rows = main(account, database, csv_file)
    print(f"Inserted {inserted_rows} validated row(s).")
