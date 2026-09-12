from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import requests

from brightpearl.common import Credentials, connect_sqlite, ensure_account_binding
from brightpearl.inventory_import import update_product_catalogue
from brightpearl.inventory_pricelists import sync_inventory_pricelists
from brightpearl.settings import get_settings
from brightpearl.warehouse_locations import update_location_catalogue
from reference_data import fetch_and_store_reference_tables
from validator import validate_and_enrich_inventory

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
MAX_PAGE_SIZE = 500
SUPPORTED_REGIONS = {"euw1", "use1"}
INVENTORY_HEADERS = ["sku", "quantity", "locationName", "costprice", "warehouseId"]


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


def save_account(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
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


def credentials_for_account(store: Store, account_name: str) -> Credentials:
    account = normalize_account_name(account_name)
    credentials = read_credential(account)
    if credentials is None:
        raise WorkerError("credentials_missing", "Save Brightpearl credentials before syncing this account.")
    meta = account_summary_without_secret(store, account)
    return Credentials(credentials.app_ref, credentials.token, meta["region"])


def validate_account(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    account_name = normalize_account_name(params.get("accountName"))
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
    credentials = credentials_for_account(store, account_name)
    db_path = account_data_db(store, account_name)
    base_currency = account_summary_without_secret(store, account_name).get("baseCurrency")
    prepare_legacy_credentials(store, account_name, credentials, base_currency)
    update_job(store, request_id, kind="reference_sync", state="running", source_path=account_name)
    logs: list[str] = []

    def worker_log(message: str) -> None:
        logs.append(str(message))
        if len(logs) > 100:
            del logs[: len(logs) - 100]
        emit_event(request_id, "progress", {"message": str(message)})

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
    path = Path(str(params.get("path", ""))).expanduser()
    if not path.exists() or not path.is_file():
        raise WorkerError("source_missing", "Choose an existing local CSV or XLSX file.")
    if path.suffix.lower() not in {".csv", ".xlsx"}:
        raise WorkerError("unsupported_source", "Inventory import accepts CSV or XLSX files.")
    credentials = credentials_for_account(store, account_name)
    db_path = account_data_db(store, account_name)
    prepare_legacy_credentials(store, account_name, credentials, account_summary_without_secret(store, account_name).get("baseCurrency"))
    update_job(store, request_id, kind="inventory_validation", state="running", source_path=str(path))
    logs: list[str] = []

    def worker_log(message: str) -> None:
        logs.append(str(message))
        if len(logs) > 100:
            del logs[: len(logs) - 100]

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
        with consolidated_rejections.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if rows:
            source_fields = [
                field
                for field in rows[0]
                if field not in {"validation_categories", "validation_error"}
            ]
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("w", encoding="utf-8-sig", newline="") as report_handle:
                writer = csv.DictWriter(
                    report_handle, fieldnames=["category", "reason", *source_fields]
                )
                writer.writeheader()
                for row in rows:
                    detailed_row = {
                        "category": row.pop("validation_categories", ""),
                        "reason": row.pop("validation_error", ""),
                        **row,
                    }
                    writer.writerow(detailed_row)
                    if len(rejected) < limit:
                        rejected.append(detailed_row)
        return len(rows), rejected

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
    print(line, flush=True)


def fail(request_id: Any, code: str, message: str) -> None:
    output(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "type": "response",
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
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
            "FROM jobs ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
    return {"jobs": [dict(row) for row in rows]}


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
        elif method == "saveAccount":
            succeed(request_id, save_account(store, params))
        elif method == "validateAccount":
            succeed(request_id, validate_account(store, params))
        elif method == "syncInventoryReferences":
            succeed(request_id, sync_inventory_references(store, request_id, params))
        elif method == "validateInventoryFile":
            succeed(request_id, validate_inventory_file(store, request_id, params))
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
