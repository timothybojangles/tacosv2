"""CLI helper for syncing validated warehouse zones to Brightpearl."""
from __future__ import annotations

import sys

from brightpearl.warehouse_zones import sync_zones


def main(account_name: str, db_path: str) -> int:
    """Sync validated zones for ``account_name`` using ``db_path``."""

    return sync_zones(account_name, db_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python sync_zones.py <account_name> <db_path>")
        sys.exit(1)

    account = sys.argv[1]
    database = sys.argv[2]
    created = main(account, database)
    print(f"Created {created} zone(s).")
