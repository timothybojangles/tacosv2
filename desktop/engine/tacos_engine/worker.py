from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes
import contextlib
import gc
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from dataclasses import asdict
from pathlib import Path
from typing import Any

import duckdb
import requests

from csv_safety import open_table
from brightpearl.common import (
    DEBUG_LOG,
    PAYLOAD_LOG,
    SYNC_DEBUG_LOG,
    Credentials,
    connect_sqlite,
    ensure_account_binding,
    mark_rows_processed,
    unprocessed_where_clause,
)
from brightpearl.additional_addresses import update_contact_catalogue
from brightpearl.throttle import parse_int_header, throttle_decision
from brightpearl.inventory_import import update_product_catalogue
from brightpearl.inventory_pricelists import sync_inventory_pricelists
from brightpearl.settings import AppSettings, get_settings, normalize_settings, save_settings, settings_path
from brightpearl.warehouse_locations import update_location_catalogue
from reference_data import fetch_and_store_reference_tables
from validator import validate_and_enrich_inventory
from validator_sales_orders import (
    ORDERS_TABLE as SALES_ORDERS_TABLE,
    ROWS_TABLE as SALES_ROWS_TABLE,
    TEMPLATE_HEADERS as SALES_TEMPLATE_HEADERS,
    validate_sales_orders,
)
from sync_sales_orders import (
    build_bp_order_payload as build_sales_order_payload,
    build_bp_payment_payload as build_sales_payment_payload,
    load_validated_orders as load_validated_sales_orders,
    post_order as post_sales_order,
    post_payment as post_sales_payment,
    update_order_id as update_sales_order_id,
)

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
MAX_PAGE_SIZE = 500
SUPPORTED_REGIONS = {"euw1", "use1"}
INVENTORY_HEADERS = ["sku", "quantity", "locationName", "costprice", "warehouseId"]
DESKTOP_APP_VERSION = "0.1.5"
LEGACY_APP_VERSION = "1.3.007"
KNOWN_LOGS = {
    "api": {"label": "Brightpearl API", "fileName": DEBUG_LOG},
    "sync": {"label": "Sync", "fileName": SYNC_DEBUG_LOG},
    "payload": {"label": "Payload", "fileName": PAYLOAD_LOG},
}

LEGACY_OPERATIONS: list[dict[str, Any]] = [
    {
        "id": "inventory_import",
        "label": "Inventory Import",
        "category": "Go Live",
        "legacyView": "inventory_import",
        "status": "active",
        "legacyModules": ["validator.py", "sync.py", "brightpearl/inventory_import.py", "brightpearl/inventory_pricelists.py"],
        "apiFamilies": ["Product search", "Warehouse search/location", "Product availability", "Stock correction"],
        "workflow": ["Sync references", "Validate CSV/XLSX", "Preview corrections", "Run confirmed stock corrections"],
    },
    {
        "id": "open_sales",
        "label": "Open Sales",
        "category": "Go Live",
        "legacyView": "open_sales",
        "status": "active",
        "legacyModules": ["validator_sales_orders.py", "sync_sales_orders.py"],
        "apiFamilies": ["Order POST", "Sales order rows", "Sales payments"],
        "workflow": ["Validate order groups", "Create sales orders", "Add rows", "Checkpoint payments"],
    },
    {
        "id": "open_purchases",
        "label": "Open Purchases",
        "category": "Go Live",
        "legacyView": "open_purchases",
        "status": "foundation",
        "legacyModules": ["validator_open_purchases.py", "sync_open_purchases.py"],
        "apiFamilies": ["Order POST", "Purchase order rows", "Purchase payments"],
        "workflow": ["Validate purchase groups", "Create purchase orders", "Add rows", "Checkpoint payments"],
    },
    {
        "id": "historic_sales",
        "label": "Historic Sales",
        "category": "Go Live",
        "legacyView": "historic_sales",
        "status": "foundation",
        "legacyModules": ["validator_historic_orders.py", "sync_historic_orders.py"],
        "apiFamilies": ["Order POST", "Order rows", "Sales payments"],
        "workflow": ["Validate historic order groups", "Create closed/history orders", "Add rows", "Checkpoint payments"],
    },
    {
        "id": "contact_import",
        "label": "Contact Import",
        "category": "Configuration",
        "legacyView": "contact_import",
        "status": "foundation",
        "legacyModules": ["validator_contacts.py", "sync_contacts.py", "brightpearl/additional_addresses.py"],
        "apiFamilies": ["Contact search", "Postal address", "Contact create/update"],
        "workflow": ["Sync contact catalogue", "Validate contacts/addresses", "Create addresses", "Create/link contacts"],
    },
    {
        "id": "product_import_update",
        "label": "Product Import and Update",
        "category": "Configuration",
        "legacyView": "product_import",
        "status": "foundation",
        "legacyModules": ["brightpearl/product_import.py", "row_validation.py"],
        "apiFamilies": ["Product references", "Product create", "Product update", "Product options/variations/bundles"],
        "workflow": ["Sync references", "Validate products", "Create missing references", "Create/update products"],
    },
    {
        "id": "custom_fields",
        "label": "Custom Fields",
        "category": "Configuration",
        "legacyView": "custom_fields",
        "status": "foundation",
        "legacyModules": ["brightpearl/custom_fields.py"],
        "apiFamilies": ["Custom field metadata", "Contact/Product/Order custom fields", "Order catalogue"],
        "workflow": ["Sync metadata", "Validate typed values", "Preview patches", "Apply custom field updates"],
    },
    {
        "id": "warehouse_locations_zones",
        "label": "Warehouse Locations and Zones",
        "category": "Configuration",
        "legacyView": "warehouse_locations",
        "status": "foundation",
        "legacyModules": ["brightpearl/warehouse_locations.py", "brightpearl/warehouse_zones.py"],
        "apiFamilies": ["Warehouse search", "Location search/create/update", "Zone search/create"],
        "workflow": ["Sync warehouse references", "Validate locations/zones", "Create or update records", "Export reference CSVs"],
    },
    {
        "id": "gdpr_forget",
        "label": "Forget Contacts",
        "category": "Maintenance",
        "legacyView": "forget_contact",
        "status": "foundation",
        "legacyModules": ["forget_contacts.py", "brightpearl/forget_contact.py"],
        "apiFamilies": ["Contact search", "Forget contact", "Related orders"],
        "workflow": ["Sync forget catalogue", "Preview targets", "Confirm irreversible action", "Record outcomes"],
    },
    {
        "id": "warehouse_service_maintenance",
        "label": "Warehouse Service Maintenance",
        "category": "Maintenance",
        "legacyView": "warehouse_service_maintenance",
        "status": "foundation",
        "legacyModules": ["brightpearl/warehouse_service_maintenance.py"],
        "apiFamilies": ["Product availability", "Stock correction", "Goods note correction"],
        "workflow": ["Import task CSV", "Preview warehouse stock moves", "Run checkpointed corrections", "Review notes"],
    },
    {
        "id": "product_catalogue_export",
        "label": "Product Catalogue Export",
        "category": "Export",
        "legacyView": "export_product_catalogue",
        "status": "foundation",
        "legacyModules": ["brightpearl/product_catalogue_export.py"],
        "apiFamilies": ["Product search", "Product details", "Suppliers", "Price lists"],
        "workflow": ["Sync export dataset", "Refresh suppliers/prices", "Filter dataset", "Export CSV"],
    },
    {
        "id": "ip_stock_history",
        "label": "IP Stock History",
        "category": "Export",
        "legacyView": "ip_stock_history",
        "status": "foundation",
        "legacyModules": ["brightpearl/ip_stock_history.py"],
        "apiFamilies": ["Local audit import", "Warehouse/date export"],
        "workflow": ["Import audit CSV", "Validate dates/warehouses", "Filter stock history", "Export by warehouse"],
    },
    {
        "id": "api_shooter",
        "label": "API Shooter and Chains",
        "category": "API Tools",
        "legacyView": "shoot_apis",
        "status": "foundation",
        "legacyModules": ["brightpearl/shoot_api.py"],
        "apiFamilies": ["Any Brightpearl endpoint", "Variables", "Response mappings", "Saved chains"],
        "workflow": ["Build request", "Map variables", "Run single/bulk requests", "Chain ordered steps"],
    },
    {
        "id": "training_helper",
        "label": "IC Training Helper",
        "category": "Training",
        "legacyView": "ic_training_helper",
        "status": "foundation",
        "legacyModules": ["brightpearl/ic_training_helper.py"],
        "apiFamilies": ["Dummy contacts/products", "Sales orders", "Payments", "Stock corrections"],
        "workflow": ["Check defaults", "Create demo data", "Create demo orders", "Allocate demo inventory"],
    },
]

GO_LIVE_OPERATIONS = [operation for operation in LEGACY_OPERATIONS if operation["category"] == "Go Live"]

ERROR_GUIDANCE = {
    "credentials_missing": "Open Accounts, save credentials for this account, then try again.",
    "credential_check_failed": "Check the account name, region, app ref and token. If they look right, try again after confirming Brightpearl is reachable.",
    "source_missing": "Choose the source file again. It may have been moved, renamed or deleted.",
    "unsupported_source": "Use one of the supported file types for this tool.",
    "invalid_price_list": "Sync required data, then pick a price list that belongs to this account.",
    "stale_validation": "Validate the source again so the preview matches the latest references.",
    "confirmation_required": "Type the account name exactly as shown, including hyphens or underscores.",
    "resume_required": "Finish, resume or reconcile the current run before starting another one.",
    "reconciliation_required": "Check Brightpearl for the last attempted write, then mark or rerun only after you know the outcome.",
    "write_uncertain": "Do not retry blindly. Check Brightpearl first so the same business action is not sent twice.",
    "report_missing": "Rebuild the preview or exception report; the temporary file is no longer available.",
    "worker_error": "Try the action again. If it repeats, check Job History and send the exact message to support.",
}


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _DiscardLegacyOutput:
    """Keep legacy console logging away from the JSON protocol stream."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


@dataclass
class Store:
    root: Path
    ledger: Path
    datasets: Path
    accounts: Path


cancelled: set[str] = set()
cancel_lock = threading.Lock()


def app_store() -> Store:
    configured = os.environ.get("TACOS_DESKTOP_DATA_DIR")
    if configured:
        root = Path(configured)
    else:
        root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "TACOSv2"
    datasets = root / "datasets"
    accounts = root / "accounts"
    datasets.mkdir(parents=True, exist_ok=True)
    accounts.mkdir(parents=True, exist_ok=True)
    ledger = root / "jobs.sqlite"
    with sqlite3.connect(ledger) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                source_path TEXT,
                dataset_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                message TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_name TEXT PRIMARY KEY,
                region TEXT NOT NULL,
                base_currency TEXT,
                last_validated_at REAL,
                last_reference_sync_at REAL,
                updated_at REAL NOT NULL
            )
            """
        )
    recover_account_index(root, ledger)
    return Store(root=root, ledger=ledger, datasets=datasets, accounts=accounts)


def recover_account_index(root: Path, ledger: Path) -> None:
    compatibility_db = root / "db" / "credentials.db"
    if not compatibility_db.is_file():
        return
    try:
        with sqlite3.connect(compatibility_db) as source:
            rows = source.execute(
                "SELECT account_name, region, base_currency FROM credentials"
            ).fetchall()
    except sqlite3.Error:
        return
    now = time.time()
    with sqlite3.connect(ledger) as destination:
        for account_name, region, base_currency in rows:
            if region not in SUPPORTED_REGIONS:
                continue
            destination.execute(
                """
                INSERT OR IGNORE INTO accounts
                    (account_name, region, base_currency, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (account_name, region, base_currency, now),
            )


def normalize_account_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 80 or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise WorkerError("invalid_account", "Account name must use letters, numbers, hyphens or underscores.")
    return name


def account_data_db(store: Store, account_name: str) -> Path:
    safe = normalize_account_name(account_name)
    folder = store.accounts / safe
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "brightpearl_data.sqlite"
    ensure_account_binding(str(path), safe)
    return path


def unlink_sqlite_file(path: Path) -> bool:
    if not path.exists():
        return False
    last_error: OSError | None = None
    for _ in range(8):
        gc.collect()
        try:
            path.unlink()
            return True
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    if last_error:
        raise last_error
    return False


def ensure_no_open_inventory_run(store: Store, account_name: str) -> None:
    with sqlite3.connect(account_data_db(store, account_name)) as conn:
        try:
            open_run = conn.execute("SELECT 1 FROM inventory_live_runs WHERE state = 'running' LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            open_run = None
    if open_run:
        raise WorkerError("resume_required", "This account has an unfinished live inventory run; resume or reconcile it before changing credentials, references or staged rows.")


@contextlib.contextmanager
def legacy_cwd(store: Store):
    previous = Path.cwd()
    store.root.mkdir(parents=True, exist_ok=True)
    os.chdir(store.root)
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def legacy_operation(store: Store):
    with legacy_cwd(store), contextlib.redirect_stdout(_DiscardLegacyOutput()):
        yield


def upsert_account_metadata(
    store: Store,
    account_name: str,
    region: str,
    *,
    base_currency: str | None = None,
    validated: bool = False,
    references_synced: bool = False,
) -> dict[str, Any]:
    now = time.time()
    with sqlite3.connect(store.ledger) as conn:
        conn.execute(
            """
            INSERT INTO accounts
                (account_name, region, base_currency, last_validated_at, last_reference_sync_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name) DO UPDATE SET
                region = excluded.region,
                base_currency = coalesce(excluded.base_currency, accounts.base_currency),
                last_validated_at = coalesce(excluded.last_validated_at, accounts.last_validated_at),
                last_reference_sync_at = coalesce(excluded.last_reference_sync_at, accounts.last_reference_sync_at),
                updated_at = excluded.updated_at
            """,
            (
                account_name,
                region,
                base_currency,
                now if validated else None,
                now if references_synced else None,
                now,
            ),
        )
    return account_summary(store, account_name)


def account_summary(store: Store, account_name: str) -> dict[str, Any]:
    with sqlite3.connect(store.ledger) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT account_name, region, base_currency, last_validated_at, last_reference_sync_at "
            "FROM accounts WHERE account_name = ?",
            (account_name,),
        ).fetchone()
    if not row:
        raise WorkerError("account_missing", "Account is not registered.")
    db_path = account_data_db(store, row["account_name"])
    counts = reference_counts(db_path)
    return {
        "accountName": row["account_name"],
        "region": row["region"],
        "baseCurrency": row["base_currency"],
        "lastValidatedAt": row["last_validated_at"],
        "lastReferenceSyncAt": row["last_reference_sync_at"],
        "credentialStatus": "saved" if read_credential(row["account_name"]) else "missing",
        "dataDbPath": str(db_path),
        "referenceCounts": counts,
    }


def reference_counts(db_path: Path) -> dict[str, int]:
    tables = {
        "products": "product_catalogue",
        "warehouses": "ref_warehouses",
        "locations": "ref_locations",
        "priceLists": "ref_price_lists",
        "priceListValues": "ref_price_list_values",
        "validatedInventory": "validated_inventory",
    }
    result = {key: 0 for key in tables}
    with sqlite3.connect(db_path) as conn:
        for key, table in tables.items():
            try:
                result[key] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                result[key] = 0
    return result


def sales_reference_counts(db_path: Path) -> dict[str, int]:
    tables = {
        "customers": "contact_catalogue",
        "products": "product_catalogue",
        "warehouses": "ref_warehouses",
        "channels": "ref_channels",
        "priceLists": "ref_price_lists",
        "currencies": "ref_currencies",
        "shippingMethods": "ref_shipping_methods",
        "paymentMethods": "ref_payment_methods",
        "orderStatuses": "ref_order_statuses",
        "validatedOrders": SALES_ORDERS_TABLE,
        "validatedRows": SALES_ROWS_TABLE,
    }
    result = {key: 0 for key in tables}
    with sqlite3.connect(db_path) as conn:
        for key, table in tables.items():
            try:
                result[key] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                result[key] = 0
    return result


def reference_sync_progress(db_path: Path, reference_name: str) -> dict[str, Any]:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, completed, total, message FROM reference_sync_progress WHERE reference_name = ?",
                (reference_name,),
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    status, completed, total, message = row
    progress: dict[str, Any] = {
        "stage": reference_name,
        "status": status,
        "completed": int(completed or 0),
        "message": message,
    }
    if total is not None:
        progress["total"] = int(total)
        if int(total) > 0:
            progress["percent"] = min(100, int((int(completed or 0) / int(total)) * 100))
    return progress


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
        ("TargetName", ctypes.wintypes.LPWSTR),
        ("Comment", ctypes.wintypes.LPWSTR),
        ("LastWritten", ctypes.wintypes.FILETIME),
        ("CredentialBlobSize", ctypes.wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", ctypes.wintypes.DWORD),
        ("AttributeCount", ctypes.wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.wintypes.LPWSTR),
        ("UserName", ctypes.wintypes.LPWSTR),
    ]


def credential_target(account_name: str) -> str:
    return f"TACOSv2/Brightpearl/{normalize_account_name(account_name)}"


def save_credential(account_name: str, app_ref: str, token: str) -> None:
    payload = json.dumps({"appRef": app_ref, "token": token}, separators=(",", ":"))
    if os.name != "nt" or os.environ.get("TACOS_CREDENTIAL_BACKEND") == "sqlite_plaintext":
        store = app_store()
        with sqlite3.connect(store.ledger) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dev_credentials "
                "(account_name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO dev_credentials VALUES (?, ?)",
                (normalize_account_name(account_name), payload),
            )
        return

    blob = payload.encode("utf-16-le")
    credential = CREDENTIAL()
    credential.Type = 1
    credential.TargetName = credential_target(account_name)
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_byte))
    credential.Persist = 2
    credential.UserName = normalize_account_name(account_name)
    if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise WorkerError("credential_store_failed", "Could not save credentials to Windows Credential Manager.")


def read_credential(account_name: str) -> Credentials | None:
    account = normalize_account_name(account_name)
    if os.name != "nt" or os.environ.get("TACOS_CREDENTIAL_BACKEND") == "sqlite_plaintext":
        store = app_store()
        with sqlite3.connect(store.ledger) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dev_credentials "
                "(account_name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            row = conn.execute("SELECT payload FROM dev_credentials WHERE account_name = ?", (account,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        meta = account_summary_without_secret(store, account)
        return Credentials(payload["appRef"], payload["token"], meta["region"])

    pointer = ctypes.POINTER(CREDENTIAL)()
    if not ctypes.windll.advapi32.CredReadW(credential_target(account), 1, 0, ctypes.byref(pointer)):
        return None
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        payload = json.loads(blob.decode("utf-16-le"))
        meta = account_summary_without_secret(app_store(), account)
        return Credentials(payload["appRef"], payload["token"], meta["region"])
    finally:
        ctypes.windll.advapi32.CredFree(pointer)


def delete_credential(store: Store, account_name: str) -> None:
    account = normalize_account_name(account_name)
    if os.environ.get("TACOS_CREDENTIAL_BACKEND") == "sqlite_plaintext":
        with sqlite3.connect(store.ledger) as conn:
            conn.execute("DELETE FROM dev_credentials WHERE account_name = ?", (account,))
        return
    if not ctypes.windll.advapi32.CredDeleteW(credential_target(account), 1, 0):
        error = ctypes.windll.kernel32.GetLastError()
        if error != 1168:  # ERROR_NOT_FOUND means it is already disconnected.
            raise WorkerError("credential_delete_failed", "Could not remove credentials from Windows Credential Manager.")


def account_summary_without_secret(store: Store, account_name: str) -> dict[str, Any]:
    with sqlite3.connect(store.ledger) as conn:
        row = conn.execute(
            "SELECT region, base_currency FROM accounts WHERE account_name = ?",
            (account_name,),
        ).fetchone()
    if not row:
        raise WorkerError("account_missing", "Account is not registered.")
    return {"region": row[0], "baseCurrency": row[1]}


def prepare_legacy_credentials(store: Store, account_name: str, credentials: Credentials, base_currency: str | None) -> None:
    with legacy_cwd(store):
        db_dir = store.root / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        with connect_sqlite(str(db_dir / "credentials.db")) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    account_name TEXT PRIMARY KEY,
                    app_ref TEXT NOT NULL,
                    token TEXT NOT NULL,
                    region TEXT NOT NULL CHECK (region IN ('euw1','use1')),
                    base_currency TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO credentials (account_name, app_ref, token, region, base_currency)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_name) DO UPDATE SET
                    app_ref = excluded.app_ref,
                    token = excluded.token,
                    region = excluded.region,
                    base_currency = excluded.base_currency
                """,
                (account_name, credentials.app_ref, credentials.token, credentials.region, base_currency),
            )


def list_accounts(store: Store) -> dict[str, Any]:
    with sqlite3.connect(store.ledger) as conn:
        rows = conn.execute("SELECT account_name FROM accounts ORDER BY account_name COLLATE NOCASE").fetchall()
    return {"accounts": [account_summary(store, row[0]) for row in rows]}


def go_live_operations() -> dict[str, Any]:
    return {"operations": GO_LIVE_OPERATIONS}


def legacy_operations() -> dict[str, Any]:
    categories = []
    for operation in LEGACY_OPERATIONS:
        category = operation["category"]
        if category not in categories:
            categories.append(category)
    return {"operations": LEGACY_OPERATIONS, "categories": categories}


def settings_payload() -> dict[str, Any]:
    return {
        "settings": asdict(get_settings()),
        "settingsPath": str((Path.cwd() / settings_path()).resolve()),
        "options": {
            "logLevels": ["PAYLOAD", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            "appearanceModes": ["System", "Light", "Dark"],
            "appearanceThemes": ["Brightpearl", "Sage"],
            "stockCorrectionBatchSize": {"min": 1, "max": 500},
        },
    }


def app_settings_payload(store: Store) -> dict[str, Any]:
    with legacy_cwd(store):
        return settings_payload()


def update_app_settings(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    raw = params.get("settings") if isinstance(params.get("settings"), dict) else params
    with legacy_cwd(store):
        current = asdict(get_settings())
        current.update(raw)
        updated = normalize_settings(current)
        save_settings(updated)
        return settings_payload()


def _log_path(file_name: str) -> Path:
    base_dir = Path(get_settings().log_output_dir)
    if not base_dir.is_absolute():
        base_dir = Path.cwd() / base_dir
    return (base_dir / file_name).resolve()


def app_logs(store: Store) -> dict[str, Any]:
    logs = []
    with legacy_cwd(store):
        for log_id, meta in KNOWN_LOGS.items():
            path = _log_path(meta["fileName"])
            stat = path.stat() if path.exists() else None
            logs.append({
                "id": log_id,
                "label": meta["label"],
                "path": str(path),
                "exists": stat is not None,
                "size": stat.st_size if stat else 0,
                "updatedAt": stat.st_mtime if stat else None,
            })
    return {"logs": logs}


def _selected_log_path(params: dict[str, Any]) -> Path:
    log_id = str(params.get("id") or params.get("logId") or "")
    if log_id not in KNOWN_LOGS:
        raise WorkerError("invalid_log", "Choose one of the available log files.")
    return _log_path(KNOWN_LOGS[log_id]["fileName"])


def read_app_log(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    max_chars = min(max(int(params.get("maxChars", 60000) or 60000), 1000), 250000)
    with legacy_cwd(store):
        path = _selected_log_path(params)
        if not path.exists():
            return {"content": "", "path": str(path), "truncated": False}
        content = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(content) > max_chars
    return {"content": content[-max_chars:], "path": str(path), "truncated": truncated}


def export_app_log(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    destination = Path(str(params.get("destination", ""))).expanduser()
    if not destination.parent.exists():
        raise WorkerError("invalid_destination", "Choose a destination in an existing folder.")
    with legacy_cwd(store):
        source = _selected_log_path(params)
        if not source.exists():
            raise WorkerError("report_missing", "That log file has not been created yet.")
        shutil.copyfile(source, destination)
    return {"path": str(destination)}


def app_environment(store: Store) -> dict[str, Any]:
    with legacy_cwd(store):
        settings_file = str((Path.cwd() / settings_path()).resolve())
    return {
        "productName": "TACOS Desktop",
        "desktopVersion": DESKTOP_APP_VERSION,
        "legacyVersion": LEGACY_APP_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "pythonVersion": platform.python_version(),
        "platform": platform.platform(),
        "dataRoot": str(store.root.resolve()),
        "ledgerPath": str(store.ledger.resolve()),
        "settingsPath": settings_file,
    }


def save_account(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    ensure_no_open_inventory_run(store, account_name)
    region = str(params.get("region", "")).strip()
    app_ref = str(params.get("appRef", "")).strip()
    token = str(params.get("token", "")).strip()
    if region not in SUPPORTED_REGIONS:
        raise WorkerError("invalid_region", "Region must be euw1 or use1.")
    if not app_ref or not token:
        raise WorkerError("missing_credentials", "App ref and account token are required.")
    upsert_account_metadata(store, account_name, region)
    account_data_db(store, account_name)
    save_credential(account_name, app_ref, token)
    prepare_legacy_credentials(store, account_name, Credentials(app_ref, token, region), None)
    return {"account": account_summary(store, account_name)}


def remove_account(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    if params.get("confirmAccountName") != account_name:
        raise WorkerError("confirmation_required", "Type the selected account name to permanently remove it.")
    with sqlite3.connect(store.ledger) as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE account_name = ?", (account_name,)).fetchone():
            raise WorkerError("account_missing", "Account is not registered.")
    ensure_no_open_inventory_run(store, account_name)
    data_db_path = account_data_db(store, account_name)
    delete_credential(store, account_name)
    compatibility_db = store.root / "db" / "credentials.db"
    if compatibility_db.is_file():
        with sqlite3.connect(compatibility_db) as conn:
            conn.execute("DELETE FROM credentials WHERE account_name = ?", (account_name,))
    data_removed = False
    if data_db_path.exists():
        data_removed = unlink_sqlite_file(data_db_path)
    with sqlite3.connect(store.ledger) as conn:
        conn.execute("DELETE FROM accounts WHERE account_name = ?", (account_name,))
    return {"removed": account_name, "dataRemoved": data_removed}


def credentials_for_account(store: Store, account_name: str) -> Credentials:
    account = normalize_account_name(account_name)
    credentials = read_credential(account)
    if credentials is None:
        raise WorkerError("credentials_missing", "Save Brightpearl credentials before syncing this account.")
    meta = account_summary_without_secret(store, account)
    return Credentials(credentials.app_ref, credentials.token, meta["region"])


def validate_account(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    ensure_no_open_inventory_run(store, account_name)
    credentials = credentials_for_account(store, account_name)
    url = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "integration-service/account-configuration"
    )
    try:
        response = requests.get(url, headers=credentials.headers, verify=False, timeout=30)
        response.raise_for_status()
        payload = response.json().get("response", {})
    except requests.RequestException as exc:
        raise WorkerError("credential_check_failed", f"Brightpearl credential check failed: {exc}") from exc
    configuration = payload.get("configuration", payload) if isinstance(payload, dict) else {}
    base_currency = configuration.get("baseCurrencyCode") if isinstance(configuration, dict) else None
    account = upsert_account_metadata(store, account_name, credentials.region, base_currency=base_currency, validated=True)
    prepare_legacy_credentials(store, account_name, credentials, base_currency)
    return {"account": account, "baseCurrency": base_currency}


def sync_inventory_references(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    ensure_no_open_inventory_run(store, account_name)
    credentials = credentials_for_account(store, account_name)
    db_path = account_data_db(store, account_name)
    base_currency = account_summary_without_secret(store, account_name).get("baseCurrency")
    prepare_legacy_credentials(store, account_name, credentials, base_currency)
    update_job(store, request_id, kind="reference_sync", state="running", source_path=account_name)
    logs: list[str] = []

    def worker_log(message: str) -> None:
        message = str(message)
        logs.append(message)
        if len(logs) > 100:
            del logs[: len(logs) - 100]
        event: dict[str, Any] = {"operation": "reference_sync", "message": message}
        progress = re.fullmatch(r"PROGRESS:(\d{1,3})", message)
        product_count = re.search(r"Synced (\d+) of (\d+) products", message)
        if progress:
            event["percent"] = min(100, int(progress.group(1)))
        if product_count:
            event.update({"stage": "products", "completed": int(product_count.group(1)),
                          "total": int(product_count.group(2))})
        event.update(reference_sync_progress(db_path, "products"))
        emit_event(request_id, "progress", event)

    with legacy_operation(store):
        product_count = update_product_catalogue(
            account_name, str(db_path), log_callback=worker_log
        )
        warehouse_result = fetch_and_store_reference_tables(
            account_name,
            credentials.region,
            credentials.headers,
            str(db_path),
            reference_keys=("warehouses",),
            log_callback=worker_log,
        )
        location_count = update_location_catalogue(account_name, str(db_path), log_callback=worker_log)
        pricelist_result = sync_inventory_pricelists(account_name, str(db_path), log_callback=worker_log)

    update_job(store, request_id, kind="reference_sync", state="succeeded", message="Inventory references synced.")
    account = upsert_account_metadata(
        store,
        account_name,
        credentials.region,
        base_currency=account_summary_without_secret(store, account_name).get("baseCurrency"),
        references_synced=True,
    )
    return {
        "account": account,
        "results": {
            "products": product_count,
            "warehouses": warehouse_result.get("warehouses", 0),
            "locations": location_count,
            "priceLists": pricelist_result.get("price_lists", 0),
            "priceListValues": pricelist_result.get("price_list_values", 0),
        },
        "logs": logs,
    }


def validate_inventory_file(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    ensure_no_open_inventory_run(store, account_name)
    path = Path(str(params.get("path", ""))).expanduser()
    if not path.exists() or not path.is_file():
        raise WorkerError("source_missing", "Choose an existing local CSV or XLSX file.")
    if path.suffix.lower() not in {".csv", ".xlsx"}:
        raise WorkerError("unsupported_source", "Inventory import accepts CSV or XLSX files.")
    credentials = credentials_for_account(store, account_name)
    db_path = account_data_db(store, account_name)
    allow_zero_blanks = params.get("allowZeroBlanks", False)
    if not isinstance(allow_zero_blanks, bool):
        raise WorkerError("invalid_options", "Allow zero and blank values must be a boolean.")
    price_list_id = params.get("priceListId")
    if price_list_id is not None:
        try:
            price_list_id = int(price_list_id)
        except (TypeError, ValueError) as exc:
            raise WorkerError("invalid_price_list", "Select a synced price list.") from exc
        with sqlite3.connect(db_path) as conn:
            try:
                found = conn.execute("SELECT 1 FROM ref_price_lists WHERE priceListId = ?", (price_list_id,)).fetchone()
            except sqlite3.OperationalError:
                found = None
        if not found:
            raise WorkerError("invalid_price_list", "Selected price list is not present in this account's synced references.")
    prepare_legacy_credentials(store, account_name, credentials, account_summary_without_secret(store, account_name).get("baseCurrency"))
    update_job(store, request_id, kind="inventory_validation", state="running", source_path=str(path), dataset_id=account_name)
    logs: list[str] = []

    def worker_log(message: str) -> None:
        logs.append(str(message))
        if len(logs) > 100:
            del logs[: len(logs) - 100]
        if str(message).startswith("PROGRESS:"):
            emit_event(request_id, "progress", {"message": str(message)})

    with legacy_cwd(store):
        configured_exception_dir = Path(get_settings().unmatched_output_dir)
        exception_dir = (
            configured_exception_dir
            if configured_exception_dir.is_absolute()
            else store.root / configured_exception_dir
        ).resolve()
    exception_files = {
        "missing_required": exception_dir / f"{account_name}_inventory_missing_required_row.csv",
        "unmatched_sku": exception_dir / f"{account_name}_unmatched_skus.csv",
        "non_stock_tracked": exception_dir / f"{account_name}_non_stock_tracked.csv",
        "unmatched_location": exception_dir / f"{account_name}_unmatched_locations.csv",
    }
    consolidated_rejections = exception_dir / f"{account_name}_inventory_rejected.csv"
    for exception_file in [*exception_files.values(), consolidated_rejections]:
        exception_file.unlink(missing_ok=True)

    with legacy_operation(store):
        inserted = validate_and_enrich_inventory(
            str(path),
            str(db_path),
            account_name,
            log_callback=worker_log,
            allow_zero_blanks=allow_zero_blanks,
            price_list_id=price_list_id,
        )
    reports_dir = store.accounts / account_name / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"inventory-exceptions-{int(time.time() * 1000)}.csv"
    rejected, rejected_rows = inventory_rejection_report(
        exception_files, report_path, consolidated_rejections=consolidated_rejections
    )
    counts = reference_counts(db_path)
    update_job(
        store,
        request_id,
        kind="inventory_validation",
        state="succeeded" if rejected == 0 else "succeeded_with_warnings",
        message=f"Accepted {inserted} rows; rejected {rejected} rows.",
    )
    return {
        "inserted": inserted,
        "validationJobId": request_id,
        "allowZeroBlanks": allow_zero_blanks,
        "priceListId": price_list_id,
        "rejected": rejected,
        "totalRows": inserted + rejected,
        "account": account_summary(store, account_name),
        "referenceCounts": counts,
        "logs": logs,
        "validatedPreview": validated_inventory_preview(db_path),
        "rejectedPreview": rejected_rows,
        "exceptionReportPath": str(report_path) if rejected else None,
        "exceptionReportFileName": report_path.name if rejected else None,
    }


def open_sales_preview(db_path: Path, limit: int = 25) -> dict[str, Any]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            totals = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS orders,
                    COALESCE(SUM(CASE WHEN payment_amount IS NOT NULL THEN 1 ELSE 0 END), 0) AS payments,
                    COALESCE(SUM(payment_amount), 0) AS paymentTotal
                FROM {SALES_ORDERS_TABLE}
                """
            ).fetchone()
            row_count = int(conn.execute(f"SELECT COUNT(*) FROM {SALES_ROWS_TABLE}").fetchone()[0])
            orders = [dict(row) for row in conn.execute(
                f"""
                SELECT
                    o.order_ref AS orderRef,
                    o.contactId,
                    o.placed_on AS placedOn,
                    o.warehouseId,
                    o.channelId,
                    o.statusId,
                    o.currency,
                    o.priceListId,
                    o.payment_amount AS paymentAmount,
                    o.payment_method_code AS paymentMethodCode,
                    COUNT(r.id) AS rows,
                    COALESCE(SUM(r.row_net), 0) AS netTotal,
                    COALESCE(SUM(r.row_tax_amount), 0) AS taxTotal
                FROM {SALES_ORDERS_TABLE} o
                LEFT JOIN {SALES_ROWS_TABLE} r ON r.order_ref = o.order_ref
                GROUP BY o.id
                ORDER BY o.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()]
            lines = [dict(row) for row in conn.execute(
                f"""
                SELECT order_ref AS orderRef, line_number AS lineNumber, item_sku AS sku,
                    item_name AS itemName, item_qty AS quantity, row_net AS net, row_tax_amount AS tax,
                    productId, rowType
                FROM {SALES_ROWS_TABLE}
                ORDER BY order_ref, line_number
                LIMIT ?
                """,
                (limit * 4,),
            ).fetchall()]
        return {
            "orders": int(totals["orders"] or 0) if totals else 0,
            "rows": row_count,
            "payments": int(totals["payments"] or 0) if totals else 0,
            "paymentTotal": float(totals["paymentTotal"] or 0) if totals else 0.0,
            "previewOrders": orders,
            "previewRows": lines,
            "executionStatus": {
                "state": "checkpointed",
                "message": "Live Open Sales posting uses checkpoints: saved order ids are reused and payment failures do not recreate orders.",
            },
        }
    except sqlite3.OperationalError:
        return {
            "orders": 0,
            "rows": 0,
            "payments": 0,
            "paymentTotal": 0.0,
            "previewOrders": [],
            "previewRows": [],
            "executionStatus": {
                "state": "not_validated",
                "message": "Validate an Open Sales file before previewing staged orders.",
            },
        }


def validate_open_sales_file(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    account_meta = account_summary_without_secret(store, account_name)
    path = Path(str(params.get("path", ""))).expanduser()
    if not path.exists() or not path.is_file():
        raise WorkerError("source_missing", "Choose an existing local CSV or XLSX file.")
    if path.suffix.lower() not in {".csv", ".xlsx"}:
        raise WorkerError("unsupported_source", "Open Sales accepts CSV or XLSX files.")
    db_path = account_data_db(store, account_name)
    credentials = read_credential(account_name)
    if credentials:
        prepare_legacy_credentials(store, account_name, credentials, account_meta.get("baseCurrency"))
    update_job(store, request_id, kind="open_sales_validation", state="running", source_path=str(path), dataset_id=account_name)
    logs: list[str] = []

    def worker_log(message: str) -> None:
        logs.append(str(message))
        if len(logs) > 100:
            del logs[: len(logs) - 100]

    with legacy_operation(store):
        orders, rows = validate_sales_orders(str(path), str(db_path), account_name, log_callback=worker_log)

    preview = open_sales_preview(db_path)
    references = sales_reference_counts(db_path)
    update_job(
        store,
        request_id,
        kind="open_sales_validation",
        state="succeeded" if orders > 0 else "succeeded_with_warnings",
        message=f"Validated {orders} order(s) and {rows} row(s). Checkpointed live posting is available.",
    )
    return {
        "validationJobId": request_id,
        "orders": orders,
        "rows": rows,
        "referenceCounts": references,
        "logs": logs,
        "preview": preview,
    }


def preview_open_sales_run(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    account_summary_without_secret(store, account_name)
    db_path = account_data_db(store, account_name)
    preview = open_sales_preview(db_path)
    update_job(
        store,
        request_id,
        kind="open_sales_preview",
        state="succeeded" if preview["orders"] else "succeeded_with_warnings",
        dataset_id=account_name,
        message=f"Previewed {preview['orders']} staged Open Sales order(s).",
    )
    return {"accountName": account_name, **preview, "referenceCounts": sales_reference_counts(db_path)}


def save_open_sales_template(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    destination = Path(str(params.get("destination", ""))).expanduser().resolve()
    if destination.suffix.lower() not in {".csv", ".xlsx"} or not destination.parent.is_dir():
        raise WorkerError("invalid_destination", "Choose a CSV or XLSX destination in an existing folder.")
    with legacy_cwd(store):
        with open_table(str(destination), mode="w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(SALES_TEMPLATE_HEADERS)
    return {"path": str(destination), "headers": SALES_TEMPLATE_HEADERS}


def sync_open_sales_references(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    mode = str(params.get("mode") or "all").strip()
    if mode not in {"all", "reference", "contacts", "products"}:
        raise WorkerError("invalid_options", "Choose all, reference, contacts or products for Open Sales sync.")
    credentials = credentials_for_account(store, account_name)
    db_path = account_data_db(store, account_name)
    prepare_legacy_credentials(store, account_name, credentials, account_summary_without_secret(store, account_name).get("baseCurrency"))
    update_job(store, request_id, kind="open_sales_reference_sync", state="running", source_path=mode, dataset_id=account_name)
    logs: list[str] = []

    def worker_log(message: str) -> None:
        text = str(message)
        logs.append(text)
        if len(logs) > 100:
            del logs[: len(logs) - 100]
        if text.startswith("PROGRESS:"):
            emit_event(request_id, "progress", {"operation": "open_sales_reference_sync", "message": text})

    results: dict[str, Any] = {}
    with legacy_operation(store):
        if mode in {"all", "reference"}:
            results["reference"] = fetch_and_store_reference_tables(
                account_name,
                credentials.region,
                credentials.headers,
                str(db_path),
                log_callback=worker_log,
            )
        if mode in {"all", "contacts"}:
            results["contacts"] = update_contact_catalogue(account_name, str(db_path), log_callback=worker_log)
        if mode in {"all", "products"}:
            results["products"] = update_product_catalogue(account_name, str(db_path), log_callback=worker_log)

    update_job(
        store,
        request_id,
        kind="open_sales_reference_sync",
        state="succeeded",
        message="Open Sales reference sync complete.",
    )
    return {"accountName": account_name, "mode": mode, "results": results, "referenceCounts": sales_reference_counts(db_path), "logs": logs}


def ensure_open_sales_live_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS open_sales_live_orders (
            order_ref TEXT PRIMARY KEY,
            order_id INTEGER,
            state TEXT NOT NULL,
            payment_state TEXT,
            error TEXT,
            updated_at REAL NOT NULL
        )
        """
    )


def open_sales_live_status(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    db_path = account_data_db(store, account_name)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_open_sales_live_tables(conn)
        rows = conn.execute(
            "SELECT order_ref, order_id, state, payment_state, error, updated_at "
            "FROM open_sales_live_orders ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
        try:
            remaining = int(conn.execute(
                f"SELECT COUNT(*) FROM {SALES_ORDERS_TABLE} WHERE {unprocessed_where_clause()}"
            ).fetchone()[0])
        except sqlite3.OperationalError:
            remaining = 0
    return {"orders": [dict(row) for row in rows], "remaining": remaining}


def run_open_sales_order(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    confirm = normalize_account_name(params.get("confirmAccountName"))
    if confirm != account_name:
        raise WorkerError("confirmation_required", "Type the account name exactly before sending Open Sales orders.")
    credentials = credentials_for_account(store, account_name)
    db_path = account_data_db(store, account_name)
    prepare_legacy_credentials(store, account_name, credentials, account_summary_without_secret(store, account_name).get("baseCurrency"))

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_open_sales_live_tables(conn)
        uncertain = conn.execute(
            "SELECT order_ref FROM open_sales_live_orders WHERE state = 'unknown' LIMIT 1"
        ).fetchone()
        if uncertain:
            raise WorkerError("reconciliation_required", f"Order {uncertain['order_ref']} has an uncertain outcome. Reconcile it in Brightpearl before continuing.")

    with legacy_operation(store):
        orders = load_validated_sales_orders(str(db_path))
    total = len(orders)
    if total == 0:
        return {"done": True, "completed": 0, "remaining": 0, "total": 0, "waitMs": 0}
    order = orders[0]
    order_ref = str(order["order_ref"])

    update_job(store, request_id, kind="open_sales_live_order", state="running", dataset_id=account_name,
               message=f"Sending Open Sales order {order_ref}.")
    order_url = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "order-service/sales-order"
    )
    payment_url = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "accounting-service/customer-payment"
    )
    headers = {**credentials.headers, "Content-Type": "application/json"}
    order_id = order.get("orderId")

    with sqlite3.connect(db_path) as conn:
        ensure_open_sales_live_tables(conn)
        conn.execute(
            "INSERT INTO open_sales_live_orders (order_ref, order_id, state, payment_state, error, updated_at) "
            "VALUES (?, ?, 'running', NULL, NULL, ?) "
            "ON CONFLICT(order_ref) DO UPDATE SET state = 'running', error = NULL, updated_at = excluded.updated_at",
            (order_ref, order_id, time.time()),
        )

    if not order_id:
        payload = build_sales_order_payload(order)
        ok, created_id = post_sales_order(order_url, headers, payload)
        if not ok or not created_id:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE open_sales_live_orders SET state = 'unknown', error = ?, updated_at = ? WHERE order_ref = ?",
                    ("Order creation was not confirmed by Brightpearl.", time.time(), order_ref),
                )
            raise WorkerError("write_uncertain", f"Order {order_ref}: Brightpearl order creation was not confirmed. Reconcile before retrying.")
        order_id = int(created_id)
        update_sales_order_id(str(db_path), order_ref, order_id)
        order["orderId"] = order_id
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE open_sales_live_orders SET order_id = ?, state = 'order_created', updated_at = ? WHERE order_ref = ?",
                (order_id, time.time(), order_ref),
            )

    payment_required = False
    try:
        payment_required = float(order.get("payment_amount") or 0) > 0
    except (TypeError, ValueError):
        payment_required = False
    if payment_required and order.get("payment_method_code") and order.get("payment_date"):
        payment_payload = build_sales_payment_payload(order_id, order)
        ok, _ = post_sales_payment(payment_url, headers, payment_payload)
        if not ok:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE open_sales_live_orders SET state = 'payment_failed', payment_state = 'failed', error = ?, updated_at = ? WHERE order_ref = ?",
                    ("Payment failed. The order id is saved; retry will reuse it.", time.time(), order_ref),
                )
            raise WorkerError("write_uncertain", f"Order {order_ref}: payment failed. The order id {order_id} is saved; retry will not recreate the order.")
        payment_state = "succeeded"
    elif payment_required:
        payment_state = "skipped_missing_details"
    else:
        payment_state = "not_required"

    with sqlite3.connect(db_path) as conn:
        marked = mark_rows_processed(conn, SALES_ORDERS_TABLE, order["id"], f"Created sales order {order_id}")
        if marked != 1:
            conn.execute(
                "UPDATE open_sales_live_orders SET state = 'unknown', error = ?, updated_at = ? WHERE order_ref = ?",
                ("Brightpearl accepted the order but local completion failed.", time.time(), order_ref),
            )
            raise WorkerError("reconciliation_required", f"Order {order_ref}: Brightpearl accepted the order but local completion failed.")
        conn.execute(
            "UPDATE open_sales_live_orders SET state = 'succeeded', payment_state = ?, error = NULL, updated_at = ? WHERE order_ref = ?",
            (payment_state, time.time(), order_ref),
        )
    remaining = max(0, total - 1)
    update_job(store, request_id, kind="open_sales_live_order", state="succeeded", dataset_id=account_name,
               message=f"Open Sales order {order_ref} confirmed as Brightpearl order {order_id}.")
    return {
        "done": remaining == 0,
        "completed": 1,
        "remaining": remaining,
        "total": total,
        "orderRef": order_ref,
        "orderId": order_id,
        "paymentState": payment_state,
        "waitMs": 500,
    }


def current_inventory_validation(store: Store, account_name: str, validation_id: str) -> tuple[str, Path, int]:
    with sqlite3.connect(store.ledger) as conn:
        latest = conn.execute(
            "SELECT id, state, updated_at FROM jobs WHERE kind = 'inventory_validation' AND dataset_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (account_name,),
        ).fetchone()
        reference_sync = conn.execute(
            "SELECT last_reference_sync_at FROM accounts WHERE account_name = ?",
            (account_name,),
        ).fetchone()
    if not latest or latest[0] != validation_id or latest[1] not in {"succeeded", "succeeded_with_warnings"}:
        raise WorkerError("stale_validation", "Validate the current source for this account before previewing a run.")

    account = account_summary_without_secret(store, account_name)
    if reference_sync and reference_sync[0] and reference_sync[0] > latest[2]:
        raise WorkerError("stale_validation", "Inventory references changed after validation; validate again before previewing a run.")
    currency = str(account.get("baseCurrency") or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise WorkerError("currency_missing", "Check account credentials to sync a three-letter base currency before previewing a run.")
    db_path = account_data_db(store, account_name)
    with legacy_cwd(store):
        batch_size = get_settings().stock_correction_batch_size
    if not isinstance(batch_size, int) or batch_size < 1:
        raise WorkerError("invalid_batch_size", "Stock correction batch size must be positive.")
    return currency, db_path, batch_size


def preview_inventory_run(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    validation_id = str(params.get("validationJobId") or "")
    currency, db_path, batch_size = current_inventory_validation(store, account_name, validation_id)
    with sqlite3.connect(db_path) as conn:
        try:
            open_run = conn.execute("SELECT 1 FROM inventory_live_runs WHERE state = 'running' LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            open_run = None
    if open_run:
        raise WorkerError("resume_required", "An inventory live run is in progress for this account; resume or reconcile it before another dry run.")

    reports_dir = store.accounts / account_name / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"inventory-run-preview-{int(time.time() * 1000)}.jsonl"
    correction_count = 0
    batch_count = 0
    warehouse_count = 0
    sample_payload = None
    current_warehouse = None
    corrections: list[dict[str, Any]] = []

    def flush_batch(handle) -> None:
        nonlocal batch_count, sample_payload, corrections
        if not corrections:
            return
        payload = {"accountName": account_name, "warehouseId": str(current_warehouse), "corrections": corrections}
        handle.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
        if sample_payload is None:
            sample_payload = {**payload, "corrections": corrections[:10]}
        batch_count += 1
        corrections = []

    try:
        with sqlite3.connect(db_path) as conn, report_path.open("w", encoding="utf-8", newline="") as report:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT productId, quantity, locationId, costprice, warehouseId "
                    f"FROM validated_inventory WHERE {unprocessed_where_clause()} "
                    "ORDER BY CAST(warehouseId AS INTEGER), id"
                )
            except sqlite3.OperationalError as exc:
                raise WorkerError("no_staged_rows", "Validate inventory before previewing a run.") from exc
            for row in rows:
                try:
                    product_id = int(row["productId"])
                    warehouse_id = int(row["warehouseId"])
                    location_id = int(row["locationId"])
                    quantity = float(row["quantity"])
                    cost = float(row["costprice"])
                except (TypeError, ValueError) as exc:
                    raise WorkerError("invalid_staged_row", "A validated row has missing or invalid payload data; validate again.") from exc
                if min(product_id, warehouse_id, location_id) < 1 or not math.isfinite(quantity) or not math.isfinite(cost):
                    raise WorkerError("invalid_staged_row", "A validated row has invalid payload data; validate again.")
                if not quantity.is_integer():
                    raise WorkerError("fractional_quantity", "Brightpearl stock corrections require whole-number quantities; fix and validate the source again.")
                if current_warehouse != warehouse_id:
                    flush_batch(report)
                    current_warehouse = warehouse_id
                    warehouse_count += 1
                corrections.append({
                    "productId": product_id,
                    "quantity": int(quantity),
                    "locationId": location_id,
                    "cost": {"currency": currency, "value": cost},
                    "reason": "Stock Sync",
                })
                correction_count += 1
                if len(corrections) >= batch_size:
                    flush_batch(report)
            flush_batch(report)
        if correction_count == 0:
            raise WorkerError("no_staged_rows", "No unprocessed validated inventory rows are available for a dry run.")
    except Exception:
        report_path.unlink(missing_ok=True)
        raise

    update_job(store, request_id, kind="inventory_run_preview", state="succeeded", dataset_id=account_name,
               source_path=str(report_path),
               message=f"Dry run: {correction_count} corrections in {batch_count} batches; no API writes.")
    report_sha256 = file_sha256(report_path)
    return {
        "accountName": account_name,
        "currency": currency,
        "corrections": correction_count,
        "batches": batch_count,
        "warehouses": warehouse_count,
        "batchSize": batch_size,
        "samplePayload": sample_payload,
        "sampleLimit": 10,
        "reportPath": str(report_path),
        "reportFileName": report_path.name,
        "reportSha256": report_sha256,
        "previewJobId": request_id,
        "validationJobId": validation_id,
        "writeEnabled": False,
    }


def save_inventory_run_preview(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    reports_dir = (store.accounts / account_name / "reports").resolve()
    source = Path(str(params.get("reportPath", ""))).resolve()
    destination = Path(str(params.get("destination", ""))).expanduser().resolve()
    if not source.is_relative_to(reports_dir) or not source.is_file() or not source.name.startswith("inventory-run-preview-"):
        raise WorkerError("report_missing", "The dry-run report is no longer available for this account.")
    expected_sha256 = str(params.get("reportSha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise WorkerError("report_hash_missing", "Rebuild the dry run before exporting its payloads.")
    if file_sha256(source) != expected_sha256:
        raise WorkerError("report_changed", "The dry-run report changed after preview; rebuild it before exporting.")
    if destination.suffix.lower() != ".jsonl" or not destination.parent.is_dir():
        raise WorkerError("invalid_destination", "Choose a JSONL destination in an existing folder.")
    if source != destination:
        shutil.copyfile(source, destination)
    return {"path": str(destination), "sha256": expected_sha256}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_inventory_batch(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    if params.get("confirmAccountName") != account_name:
        raise WorkerError("confirmation_required", "Type the selected account name to confirm live stock corrections.")
    validation_id = str(params.get("validationJobId") or "")
    currency, db_path, batch_size = current_inventory_validation(store, account_name, validation_id)
    preview_id = str(params.get("previewJobId") or "")
    report_hash = str(params.get("reportSha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", report_hash):
        raise WorkerError("report_hash_missing", "Build a new dry run before starting live corrections.")
    with sqlite3.connect(store.ledger) as conn:
        preview_job = conn.execute(
            "SELECT source_path, state FROM jobs WHERE id = ? AND kind = 'inventory_run_preview' AND dataset_id = ?",
            (preview_id, account_name),
        ).fetchone()
    if not preview_job or preview_job[1] != "succeeded":
        raise WorkerError("preview_missing", "The account-bound dry run is no longer available.")
    reports_dir = (store.accounts / account_name / "reports").resolve()
    report_path = Path(preview_job[0]).resolve()
    if not report_path.is_relative_to(reports_dir) or not report_path.is_file():
        raise WorkerError("report_missing", "The dry-run report is no longer available for this account.")

    credentials = credentials_for_account(store, account_name)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS inventory_live_runs (
                report_sha256 TEXT PRIMARY KEY,
                preview_job_id TEXT NOT NULL,
                validation_job_id TEXT NOT NULL,
                total_batches INTEGER NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory_live_batches (
                report_sha256 TEXT NOT NULL,
                batch_index INTEGER NOT NULL,
                warehouse_id INTEGER NOT NULL,
                row_ids TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                goods_note_ids TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (report_sha256, batch_index)
            );
        """)
        uncertain = conn.execute(
            "SELECT 1 FROM inventory_live_batches WHERE state IN ('sending', 'unknown') LIMIT 1"
        ).fetchone()
        if uncertain:
            raise WorkerError("reconciliation_required", "A previous stock correction has an uncertain outcome. Reconcile it in Brightpearl before any more writes.")
        run = conn.execute(
            "SELECT preview_job_id, validation_job_id, total_batches, state FROM inventory_live_runs WHERE report_sha256 = ?",
            (report_hash,),
        ).fetchone()
        if run and (run[0] != preview_id or run[1] != validation_id):
            raise WorkerError("already_run", "This exact dry-run payload has already been started under another validation; it cannot be submitted again.")
        if run and run[3] == "complete":
            raise WorkerError("already_run", "This dry-run payload has already been sent to Brightpearl.")
        other_open = conn.execute(
            "SELECT 1 FROM inventory_live_runs WHERE report_sha256 != ? AND state != 'complete' LIMIT 1",
            (report_hash,),
        ).fetchone()
        if other_open:
            raise WorkerError("resume_required", "Finish or reconcile the previous live run for this account before starting another.")
        if not run:
            if file_sha256(report_path) != report_hash:
                raise WorkerError("report_changed", "The dry-run report changed; build a new dry run before live corrections.")
            with report_path.open("r", encoding="utf-8") as report:
                total_batches = sum(1 for _ in report)
            if total_batches < 1:
                raise WorkerError("report_empty", "The dry-run report has no batches.")
        else:
            total_batches = run[2]
        batch_index = conn.execute(
            "SELECT COUNT(*) FROM inventory_live_batches WHERE report_sha256 = ? AND state = 'succeeded'",
            (report_hash,),
        ).fetchone()[0]
        if batch_index >= total_batches:
            conn.execute("UPDATE inventory_live_runs SET state = 'complete' WHERE report_sha256 = ?", (report_hash,))
            return {"done": True, "completedBatches": total_batches, "totalBatches": total_batches, "waitMs": 0}
        with report_path.open("r", encoding="utf-8") as report:
            for _ in range(batch_index):
                next(report)
            expected = json.loads(next(report))
        first = conn.execute(
            f"SELECT warehouseId FROM validated_inventory WHERE {unprocessed_where_clause()} "
            "ORDER BY CAST(warehouseId AS INTEGER), id LIMIT 1"
        ).fetchone()
        if not first:
            raise WorkerError("staged_rows_changed", "Unprocessed staged rows no longer match the dry run.")
        warehouse_id = int(first[0])
        rows = conn.execute(
            "SELECT id, productId, quantity, locationId, costprice FROM validated_inventory "
            f"WHERE {unprocessed_where_clause()} AND CAST(warehouseId AS INTEGER) = ? ORDER BY id LIMIT ?",
            (warehouse_id, batch_size),
        ).fetchall()
        row_ids = [row["id"] for row in rows]
        corrections = []
        for row in rows:
            quantity = float(row["quantity"])
            if not math.isfinite(quantity) or not quantity.is_integer():
                raise WorkerError("fractional_quantity", "Brightpearl stock corrections require whole-number quantities.")
            corrections.append({
                "productId": int(row["productId"]), "quantity": int(quantity),
                "locationId": int(row["locationId"]),
                "cost": {"currency": currency, "value": float(row["costprice"])},
                "reason": "Stock Sync",
            })
        payload = {"accountName": account_name, "warehouseId": str(warehouse_id), "corrections": corrections}
        if payload != expected:
            raise WorkerError("staged_rows_changed", "Staged rows no longer match the reviewed dry-run payload; no write was sent.")
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        if not run:
            conn.execute(
                "INSERT INTO inventory_live_runs VALUES (?, ?, ?, ?, 'running')",
                (report_hash, preview_id, validation_id, total_batches),
            )
        conn.execute(
            "INSERT INTO inventory_live_batches VALUES (?, ?, ?, ?, ?, 'sending', NULL, ?)",
            (report_hash, batch_index, warehouse_id, json.dumps(row_ids), payload_hash, time.time()),
        )

    update_job(store, request_id, kind="inventory_live_batch", state="running", dataset_id=account_name,
               message=f"Sending batch {batch_index + 1} of {total_batches} to warehouse {warehouse_id}.")
    url = (f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}"
           f"/warehouse-service/warehouse/{warehouse_id}/stock-correction")
    try:
        response = requests.post(url, json={"corrections": corrections}, headers=credentials.headers, timeout=(10, 60))
        if response.status_code != 200:
            raise WorkerError("write_uncertain", f"Batch {batch_index + 1}/{total_batches}, warehouse {warehouse_id}: Brightpearl returned HTTP {response.status_code}; reconcile before continuing. No automatic retry was made.")
        body = response.json()
        note_ids = body.get("response") if isinstance(body, dict) else None
        if not isinstance(note_ids, list) or len(note_ids) != len(corrections) or any(type(note_id) is not int for note_id in note_ids):
            raise WorkerError("write_uncertain", f"Batch {batch_index + 1}/{total_batches}, warehouse {warehouse_id}: Brightpearl returned unexpected note IDs; reconcile before continuing.")
    except (requests.RequestException, ValueError) as exc:
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE inventory_live_batches SET state = 'unknown', updated_at = ? WHERE report_sha256 = ? AND batch_index = ?",
                         (time.time(), report_hash, batch_index))
        raise WorkerError("write_uncertain", f"Batch {batch_index + 1}/{total_batches}, warehouse {warehouse_id}: outcome unknown; reconcile in Brightpearl before continuing. No automatic retry was made.") from exc
    except WorkerError:
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE inventory_live_batches SET state = 'unknown', updated_at = ? WHERE report_sha256 = ? AND batch_index = ?",
                         (time.time(), report_hash, batch_index))
        raise

    with sqlite3.connect(db_path) as conn:
        marked = mark_rows_processed(conn, "validated_inventory", row_ids,
                                     f"Brightpearl stock correction notes {','.join(map(str, note_ids))}")
        if marked != len(row_ids):
            raise WorkerError("reconciliation_required", "Brightpearl accepted the batch but local processing could not be confirmed. Reconcile before continuing.")
        conn.execute(
            "UPDATE inventory_live_batches SET state = 'succeeded', goods_note_ids = ?, updated_at = ? "
            "WHERE report_sha256 = ? AND batch_index = ?",
            (json.dumps(note_ids), time.time(), report_hash, batch_index),
        )
        done = batch_index + 1 == total_batches
        if done:
            conn.execute("UPDATE inventory_live_runs SET state = 'complete' WHERE report_sha256 = ?", (report_hash,))
    remaining = parse_int_header(response.headers, "brightpearl-requests-remaining", 0)
    throttle_ms = parse_int_header(response.headers, "brightpearl-next-throttle-period", 0)
    wait_ms, _ = throttle_decision(remaining, throttle_ms)
    update_job(store, request_id, kind="inventory_live_batch", state="succeeded", dataset_id=account_name,
               message=f"Batch {batch_index + 1}/{total_batches} confirmed; {len(row_ids)} rows processed.")
    return {"done": done, "completedBatches": batch_index + 1, "totalBatches": total_batches,
            "corrections": len(row_ids), "warehouseId": warehouse_id, "goodsNoteIds": note_ids,
            "waitMs": max(500, wait_ms) if not done else 0}


def inventory_live_status(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    db_path = account_data_db(store, account_name)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT b.batch_index, b.warehouse_id, b.state, b.row_ids, b.goods_note_ids, b.updated_at "
                "FROM inventory_live_batches b ORDER BY b.updated_at DESC LIMIT 100"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return {"batches": [{
        "batchIndex": row["batch_index"] + 1,
        "warehouseId": row["warehouse_id"],
        "state": row["state"],
        "rowCount": len(json.loads(row["row_ids"])),
        "goodsNoteIds": json.loads(row["goods_note_ids"]) if row["goods_note_ids"] else [],
        "updatedAt": row["updated_at"],
    } for row in rows]}


def inventory_run_resume(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    db_path = account_data_db(store, account_name)
    with sqlite3.connect(db_path) as conn:
        try:
            run = conn.execute(
                "SELECT report_sha256, preview_job_id, validation_job_id, total_batches "
                "FROM inventory_live_runs WHERE state = 'running' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            completed = conn.execute(
                "SELECT COUNT(*) FROM inventory_live_batches WHERE report_sha256 = ? AND state = 'succeeded'",
                (run[0],),
            ).fetchone()[0] if run else 0
        except sqlite3.OperationalError:
            run = None
            completed = 0
    if not run:
        return {"preview": None}
    with sqlite3.connect(store.ledger) as conn:
        job = conn.execute(
            "SELECT source_path FROM jobs WHERE id = ? AND kind = 'inventory_run_preview' AND dataset_id = ?",
            (run[1], account_name),
        ).fetchone()
    if not job or not job[0] or not Path(job[0]).is_file():
        raise WorkerError("report_missing", "The interrupted live run's dry-run report is missing; reconcile before continuing.")
    report_path = Path(job[0])
    first_payload = None
    correction_count = 0
    warehouse_ids: set[str] = set()
    with report_path.open("r", encoding="utf-8") as report:
        for line in report:
            payload = json.loads(line)
            first_payload = first_payload or payload
            correction_count += len(payload["corrections"])
            warehouse_ids.add(str(payload["warehouseId"]))
    currency = account_summary_without_secret(store, account_name).get("baseCurrency")
    return {"preview": {
        "accountName": account_name, "previewJobId": run[1], "validationJobId": run[2],
        "reportSha256": run[0], "reportPath": str(report_path), "reportFileName": report_path.name,
        "corrections": correction_count, "batches": run[3], "warehouses": len(warehouse_ids),
        "currency": currency, "samplePayload": {
            **first_payload, "corrections": first_payload["corrections"][:10]
        } if first_payload else None,
    }, "completedBatches": completed}


def inventory_price_lists(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    db_path = account_data_db(store, account_name)
    with sqlite3.connect(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT priceListId, COALESCE(name, code, CAST(priceListId AS TEXT)) "
                "FROM ref_price_lists ORDER BY name, priceListId"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return {"priceLists": [{"id": row[0], "name": row[1]} for row in rows]}


def inventory_rejection_report(
    exception_files: dict[str, Path],
    report_path: Path,
    limit: int = 100,
    consolidated_rejections: Path | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    reasons = {
        "missing_required": "Missing or invalid required value",
        "unmatched_sku": "SKU was not found in the synced product catalogue",
        "non_stock_tracked": "Product is not stock tracked",
        "unmatched_location": "Warehouse or location was not found in synced references",
    }
    if consolidated_rejections and consolidated_rejections.exists():
        rejected: list[dict[str, Any]] = []
        count = 0
        with consolidated_rejections.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            source_fields = [
                field
                for field in (reader.fieldnames or [])
                if field not in {"validation_categories", "validation_error"}
            ]
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("w", encoding="utf-8-sig", newline="") as report_handle:
                writer = csv.DictWriter(
                    report_handle, fieldnames=["category", "reason", *source_fields]
                )
                writer.writeheader()
                for row in reader:
                    count += 1
                    detailed_row = {
                        "category": row.pop("validation_categories", ""),
                        "reason": row.pop("validation_error", ""),
                        **row,
                    }
                    writer.writerow(detailed_row)
                    if len(rejected) < limit:
                        rejected.append(detailed_row)
        if count == 0:
            report_path.unlink(missing_ok=True)
        return count, rejected

    source_fields: list[str] = []
    for path in exception_files.values():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for field in csv.DictReader(handle).fieldnames or []:
                if field != "validation_error" and field not in source_fields:
                    source_fields.append(field)

    rejected_count = 0
    rejected: list[dict[str, Any]] = []
    writer = None
    report_handle = None
    try:
        for category, path in exception_files.items():
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    rejected_count += 1
                    validation_error = row.pop("validation_error", "").strip()
                    detailed_row = {
                        "category": category,
                        "reason": validation_error or reasons[category],
                        **row,
                    }
                    if writer is None:
                        report_handle = report_path.open("w", encoding="utf-8-sig", newline="")
                        writer = csv.DictWriter(report_handle, fieldnames=["category", "reason", *source_fields])
                        writer.writeheader()
                    writer.writerow(detailed_row)
                    if len(rejected) < limit:
                        rejected.append(detailed_row)
    finally:
        if report_handle is not None:
            report_handle.close()
    return rejected_count, rejected


def save_inventory_exception_report(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
    reports_dir = (store.accounts / account_name / "reports").resolve()
    source = Path(str(params.get("reportPath", ""))).resolve()
    destination = Path(str(params.get("destination", ""))).expanduser().resolve()
    if not source.is_relative_to(reports_dir) or not source.is_file():
        raise WorkerError("report_missing", "The exception report is no longer available.")
    if destination.suffix.lower() != ".csv" or not destination.parent.is_dir():
        raise WorkerError("invalid_destination", "Choose a CSV destination in an existing folder.")
    if source != destination:
        shutil.copyfile(source, destination)
    return {"path": str(destination)}


def validated_inventory_preview(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT sku, quantity, locationId, costprice, warehouseId, productId, stockTracked "
                "FROM validated_inventory ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def output(payload: dict[str, Any]) -> None:
    # Protocol frames stay ASCII-safe even when Windows inherits a legacy code page.
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        line = json.dumps(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "type": "response",
                "id": payload.get("id"),
                "ok": False,
                "error": {
                    "code": "message_too_large",
                    "message": "Worker response exceeded the pipe message limit.",
                },
            },
            separators=(",", ":"),
        )
    if isinstance(sys.stdout, _DiscardLegacyOutput):
        sys.__stdout__.write(line + "\n")
        sys.__stdout__.flush()
    else:
        print(line, flush=True)


def fail(request_id: Any, code: str, message: str) -> None:
    output(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "type": "response",
            "id": request_id,
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "guidance": ERROR_GUIDANCE.get(code, ERROR_GUIDANCE["worker_error"]),
            },
        }
    )


def succeed(request_id: str, result: dict[str, Any]) -> None:
    output(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "type": "response",
            "id": request_id,
            "ok": True,
            "result": result,
        }
    )


def emit_event(request_id: str, name: str, data: dict[str, Any]) -> None:
    output(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "type": "event",
            "id": request_id,
            "event": name,
            "data": data,
        }
    )


def check_cancel(request_id: str) -> None:
    with cancel_lock:
        if request_id in cancelled:
            raise WorkerError("cancelled", "The job was cancelled.")


def update_job(store: Store, job_id: str, **values: Any) -> None:
    now = time.time()
    with sqlite3.connect(store.ledger) as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, kind, state, source_path, dataset_id, created_at, updated_at, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                source_path = coalesce(excluded.source_path, jobs.source_path),
                dataset_id = coalesce(excluded.dataset_id, jobs.dataset_id),
                updated_at = excluded.updated_at,
                message = excluded.message
            """,
            (
                job_id,
                values.get("kind", "csv_import"),
                values.get("state", "running"),
                values.get("source_path"),
                values.get("dataset_id"),
                now,
                now,
                values.get("message"),
            ),
        )


def dataset_path(store: Store, dataset_id: str) -> Path:
    if not dataset_id.replace("-", "").replace("_", "").isalnum():
        raise WorkerError("invalid_dataset", "Dataset id contains unsupported characters.")
    return store.datasets / f"{dataset_id}.duckdb"


def import_synthetic_csv(store: Store, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(params.get("path", ""))).expanduser()
    if not path.exists() or not path.is_file():
        raise WorkerError("source_missing", "Choose an existing local CSV file.")
    if path.suffix.lower() != ".csv":
        raise WorkerError("unsupported_source", "Phase 1 accepts local CSV files only.")

    dataset_id = f"dataset_{int(time.time() * 1000)}"
    db_path = dataset_path(store, dataset_id)
    update_job(store, request_id, source_path=str(path), dataset_id=dataset_id)
    emit_event(request_id, "progress", {"stage": "reading", "rowsRead": 0})

    malformed: list[dict[str, Any]] = []
    rows_read = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise WorkerError("missing_header", "CSV must contain a header row.")

        con = duckdb.connect(str(db_path))
        try:
            con.execute("SET autoinstall_known_extensions=false")
            con.execute("SET autoload_known_extensions=false")
            con.execute("CREATE TABLE rows (row_number BIGINT, raw JSON)")
            batch: list[tuple[int, str]] = []
            for row_number, row in enumerate(reader, start=2):
                check_cancel(request_id)
                rows_read += 1
                if None in row:
                    malformed.append(
                        {
                            "rowNumber": row_number,
                            "reason": "Row has more values than headers.",
                            "sourceText": ",".join(row.get(None) or []),
                        }
                    )
                batch.append((row_number, json.dumps(row, ensure_ascii=False)))
                if len(batch) >= 1000:
                    con.executemany("INSERT INTO rows VALUES (?, ?)", batch)
                    batch.clear()
                    emit_event(request_id, "progress", {"stage": "importing", "rowsRead": rows_read})
            if batch:
                con.executemany("INSERT INTO rows VALUES (?, ?)", batch)
            con.execute("CREATE TABLE metadata AS SELECT ? AS source_path, ? AS imported_at", [str(path), time.time()])
        finally:
            con.close()

    state = "succeeded" if not malformed else "succeeded_with_warnings"
    update_job(store, request_id, state=state, message=f"Imported {rows_read} rows.")
    return {
        "datasetId": dataset_id,
        "sourcePath": str(path),
        "rowsRead": rows_read,
        "malformedRows": malformed[:25],
        "warningCount": len(malformed),
    }


def preview_dataset(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    dataset_id = str(params.get("datasetId", ""))
    page = max(1, int(params.get("page", 1)))
    page_size = min(MAX_PAGE_SIZE, max(1, int(params.get("pageSize", 50))))
    filter_text = str(params.get("filter", "")).strip().casefold()
    sort_key = str(params.get("sortKey", "")).strip()
    sort_dir = "DESC" if str(params.get("sortDir", "asc")).lower() == "desc" else "ASC"
    db_path = dataset_path(store, dataset_id)
    if not db_path.exists():
        raise WorkerError("dataset_missing", "The requested dataset is not available.")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        raw_rows = con.execute("SELECT row_number, raw::VARCHAR FROM rows ORDER BY row_number").fetchall()
    finally:
        con.close()

    records = [{"rowNumber": row_number, **json.loads(raw)} for row_number, raw in raw_rows]
    if filter_text:
        records = [
            row for row in records
            if any(filter_text in str(value).casefold() for value in row.values())
        ]
    if sort_key:
        records.sort(key=lambda row: str(row.get(sort_key, "")).casefold(), reverse=sort_dir == "DESC")
    total = len(records)
    start = (page - 1) * page_size
    return {
        "page": page,
        "pageSize": page_size,
        "totalRows": total,
        "rows": records[start:start + page_size],
    }


def job_history(store: Store) -> dict[str, Any]:
    with sqlite3.connect(store.ledger) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, kind, state, source_path, dataset_id, created_at, updated_at, message "
            "FROM jobs ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
    jobs = []
    for row in rows:
        item = dict(row)
        item["durationSeconds"] = max(0, round(float(item["updated_at"] or 0) - float(item["created_at"] or 0), 2))
        jobs.append(item)
    return {"jobs": jobs}


def handle(store: Store, request: dict[str, Any]) -> None:
    request_id = request.get("id")
    if request.get("protocolVersion") != PROTOCOL_VERSION:
        fail(request_id, "protocol_mismatch", "Unsupported protocol version.")
        return
    if not isinstance(request_id, str) or not request_id:
        fail(request_id, "invalid_request", "Request id is required.")
        return
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    try:
        if method == "importSyntheticCsv":
            succeed(request_id, import_synthetic_csv(store, request_id, params))
        elif method == "previewDataset":
            succeed(request_id, preview_dataset(store, params))
        elif method == "listAccounts":
            succeed(request_id, list_accounts(store))
        elif method == "goLiveOperations":
            succeed(request_id, go_live_operations())
        elif method == "legacyOperations":
            succeed(request_id, legacy_operations())
        elif method == "appSettings":
            succeed(request_id, app_settings_payload(store))
        elif method == "saveAppSettings":
            succeed(request_id, update_app_settings(store, params))
        elif method == "appLogs":
            succeed(request_id, app_logs(store))
        elif method == "readAppLog":
            succeed(request_id, read_app_log(store, params))
        elif method == "exportAppLog":
            succeed(request_id, export_app_log(store, params))
        elif method == "appEnvironment":
            succeed(request_id, app_environment(store))
        elif method == "saveAccount":
            succeed(request_id, save_account(store, params))
        elif method == "removeAccount":
            succeed(request_id, remove_account(store, params))
        elif method == "validateAccount":
            succeed(request_id, validate_account(store, params))
        elif method == "syncInventoryReferences":
            succeed(request_id, sync_inventory_references(store, request_id, params))
        elif method == "validateInventoryFile":
            succeed(request_id, validate_inventory_file(store, request_id, params))
        elif method == "validateOpenSalesFile":
            succeed(request_id, validate_open_sales_file(store, request_id, params))
        elif method == "previewOpenSalesRun":
            succeed(request_id, preview_open_sales_run(store, request_id, params))
        elif method == "saveOpenSalesTemplate":
            succeed(request_id, save_open_sales_template(store, params))
        elif method == "syncOpenSalesReferences":
            succeed(request_id, sync_open_sales_references(store, request_id, params))
        elif method == "runOpenSalesOrder":
            succeed(request_id, run_open_sales_order(store, request_id, params))
        elif method == "openSalesLiveStatus":
            succeed(request_id, open_sales_live_status(store, params))
        elif method == "inventoryPriceLists":
            succeed(request_id, inventory_price_lists(store, params))
        elif method == "previewInventoryRun":
            succeed(request_id, preview_inventory_run(store, request_id, params))
        elif method == "saveInventoryRunPreview":
            succeed(request_id, save_inventory_run_preview(store, params))
        elif method == "runInventoryBatch":
            succeed(request_id, run_inventory_batch(store, request_id, params))
        elif method == "inventoryLiveStatus":
            succeed(request_id, inventory_live_status(store, params))
        elif method == "inventoryRunResume":
            succeed(request_id, inventory_run_resume(store, params))
        elif method == "saveInventoryExceptionReport":
            succeed(request_id, save_inventory_exception_report(store, params))
        elif method == "jobHistory":
            succeed(request_id, job_history(store))
        elif method == "cancel":
            with cancel_lock:
                cancelled.add(str(params.get("id", request_id)))
            succeed(request_id, {"cancelled": True})
        elif method == "shutdown":
            succeed(request_id, {"shutdown": True})
            raise SystemExit(0)
        else:
            raise WorkerError("unknown_method", "Unknown worker method.")
    except WorkerError as exc:
        update_job(store, request_id, state="failed", message=exc.message)
        fail(request_id, exc.code, exc.message)
    except Exception as exc:
        update_job(store, request_id, state="failed", message=str(exc))
        fail(request_id, "worker_error", str(exc))


def main() -> int:
    store = app_store()
    output({"protocolVersion": PROTOCOL_VERSION, "type": "ready", "pid": os.getpid()})
    for line in sys.stdin:
        if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
            fail(None, "message_too_large", "Request exceeded the pipe message limit.")
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            fail(None, "invalid_json", "Request was not valid JSON.")
            continue
        handle(store, request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
