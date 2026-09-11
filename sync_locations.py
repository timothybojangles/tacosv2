"""CLI helper for syncing validated warehouse locations to Brightpearl."""
from __future__ import annotations

import sys

from brightpearl.warehouse_locations import sync_locations


def main(account_name: str, db_path: str) -> int:
    """Sync validated locations for ``account_name`` using ``db_path``."""

    return sync_locations(account_name, db_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python sync_locations.py <account_name> <db_path>")
        sys.exit(1)

    account = sys.argv[1]
    database = sys.argv[2]
    created = main(account, database)
    print(f"Created {created} location(s).")
