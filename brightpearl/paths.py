"""Shared filesystem paths for Brightpearl utilities."""
from __future__ import annotations

import os


def ensure_db_dir() -> None:
    os.makedirs("db", exist_ok=True)


def get_credentials_db_path() -> str:
    ensure_db_dir()
    return os.path.join("db", "credentials.db")


def get_data_db_path(account_name: str) -> str:
    ensure_db_dir()
    return os.path.join("db", f"{account_name}_brightpearl_data.db")
