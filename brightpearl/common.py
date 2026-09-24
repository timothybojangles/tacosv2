"""Shared helpers for interacting with the Brightpearl public API."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Tuple

import requests

from .settings import get_settings, should_log

DEBUG_LOG = "brightpearl_api.log"
SYNC_DEBUG_LOG = "sync_debug.log"
PAYLOAD_LOG = "payload_debug.log"
LogCallback = Optional[Callable[[str], None]]


PROCESSED_COLUMN = "processed"
PROCESSED_AT_COLUMN = "processedAt"
PROCESSED_DETAILS_COLUMN = "processedDetails"
PROCESSING_COLUMNS = (PROCESSED_COLUMN, PROCESSED_AT_COLUMN, PROCESSED_DETAILS_COLUMN)


def quote_sql_identifier(value: str) -> str:
    """Return a safely quoted SQLite identifier."""
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def processing_column_definitions(*, include_leading_comma: bool = True) -> str:
    """SQL column definitions for resumable CSV processing metadata."""
    prefix = "," if include_leading_comma else ""
    return (
        f"{prefix}\n            {PROCESSED_COLUMN} INTEGER NOT NULL DEFAULT 0,"
        f"\n            {PROCESSED_AT_COLUMN} TEXT,"
        f"\n            {PROCESSED_DETAILS_COLUMN} TEXT"
    )


def ensure_processing_columns(conn: sqlite3.Connection, table_name: str) -> None:
    """Add resumable-processing columns to an existing SQLite table if needed."""
    cur = conn.cursor()
    quoted_table = quote_sql_identifier(table_name)
    cur.execute(f"PRAGMA table_info({quoted_table})")
    existing = {row[1] for row in cur.fetchall()}
    if PROCESSED_COLUMN not in existing:
        cur.execute(
            f"ALTER TABLE {quoted_table} "
            f"ADD COLUMN {quote_sql_identifier(PROCESSED_COLUMN)} INTEGER NOT NULL DEFAULT 0"
        )
    if PROCESSED_AT_COLUMN not in existing:
        cur.execute(
            f"ALTER TABLE {quoted_table} "
            f"ADD COLUMN {quote_sql_identifier(PROCESSED_AT_COLUMN)} TEXT"
        )
    if PROCESSED_DETAILS_COLUMN not in existing:
        cur.execute(
            f"ALTER TABLE {quoted_table} "
            f"ADD COLUMN {quote_sql_identifier(PROCESSED_DETAILS_COLUMN)} TEXT"
        )


def unprocessed_where_clause(table_alias: str | None = None) -> str:
    """Return a WHERE predicate that selects rows not yet marked processed."""
    prefix = f"{table_alias}." if table_alias else ""
    return f"COALESCE({prefix}{quote_sql_identifier(PROCESSED_COLUMN)}, 0) != 1"


def mark_rows_processed(
    conn: sqlite3.Connection,
    table_name: str,
    row_ids: int | Sequence[int] | Iterable[int],
    details: str = "Processed successfully",
) -> int:
    """Mark one or more table rows as processed for resumable CSV runs."""
    if isinstance(row_ids, int):
        ids = [row_ids]
    else:
        ids = [int(row_id) for row_id in row_ids]
    if not ids:
        return 0

    ensure_processing_columns(conn, table_name)
    processed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    quoted_table = quote_sql_identifier(table_name)
    placeholders = ", ".join("?" for _ in ids)
    cur = conn.cursor()
    cur.execute(
        f"UPDATE {quoted_table} "
        f"SET {quote_sql_identifier(PROCESSED_COLUMN)} = 1, "
        f"{quote_sql_identifier(PROCESSED_AT_COLUMN)} = ?, "
        f"{quote_sql_identifier(PROCESSED_DETAILS_COLUMN)} = ? "
        f"WHERE id IN ({placeholders})",
        [processed_at, details, *ids],
    )
    return cur.rowcount


def mark_table_key_processed(
    conn: sqlite3.Connection,
    table_name: str,
    key_column: str,
    key_value,
    details: str = "Processed successfully",
) -> int:
    """Mark rows as processed using a table-specific key column."""
    ensure_processing_columns(conn, table_name)
    processed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.cursor()
    cur.execute(
        f"UPDATE {quote_sql_identifier(table_name)} "
        f"SET {quote_sql_identifier(PROCESSED_COLUMN)} = 1, "
        f"{quote_sql_identifier(PROCESSED_AT_COLUMN)} = ?, "
        f"{quote_sql_identifier(PROCESSED_DETAILS_COLUMN)} = ? "
        f"WHERE {quote_sql_identifier(key_column)} = ?",
        (processed_at, details, key_value),
    )
    return cur.rowcount


def connect_sqlite(db_path: str, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Create a SQLite connection configured to reduce lock contention."""
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        # Some environments/filesystems can reject WAL mode.
        pass
    return conn


@dataclass(frozen=True)
class Credentials:
    """Minimal credential information required for Brightpearl requests."""

    app_ref: str
    token: str
    region: str

    @property
    def headers(self) -> dict:
        """Return HTTP headers required for Brightpearl API calls."""
        return {
            "brightpearl-app-ref": self.app_ref,
            "brightpearl-account-token": self.token,
        }


def log(
    message: str,
    log_callback: LogCallback = None,
    *,
    level: str = "INFO",
    log_file: Optional[str] = DEBUG_LOG,
) -> None:
    """Persist debug logs and optionally mirror them to the UI."""
    if not should_log(level):
        return
    if log_file:
        resolved = _resolve_log_path(log_file)
        if resolved:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            with open(resolved, "a", encoding="utf-8") as log_handle:
                log_handle.write(message + "\n")
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", errors="replace").decode("ascii"))
    if log_callback:
        try:
            log_callback(message)
        except UnicodeEncodeError:
            log_callback(message.encode("ascii", errors="replace").decode("ascii"))


def log_sync(message: str, log_callback: LogCallback = None, *, level: str = "INFO") -> None:
    """Log to the shared sync debug log."""
    log(message, log_callback=log_callback, level=level, log_file=SYNC_DEBUG_LOG)


def log_payload(message: str, log_callback: LogCallback = None) -> None:
    """Log full payload/response data when payload logging is enabled."""
    log(message, log_callback=log_callback, level="PAYLOAD", log_file=PAYLOAD_LOG)


def format_payload_for_log(payload: object) -> str:
    """Return an untruncated, readable representation of a request payload."""
    if payload is None:
        return "-"
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


def log_payload_exchange(
    method: str,
    url: str,
    payload: object,
    response: requests.Response,
    log_callback: LogCallback = None,
) -> None:
    """Log a full request payload and response body for payload-level diagnostics."""
    log_payload(
        (
            f"API {method.upper()} {url} -> {response.status_code}\n"
            f"Payload:\n{format_payload_for_log(payload)}\n"
            f"Response:\n{response.text}"
        ),
        log_callback,
    )


def log_search_progress(metadata: dict, log_callback: LogCallback = None) -> None:
    """Emit `PROGRESS:<pct>` from search endpoint metadata when available."""
    if not isinstance(metadata, dict):
        return

    try:
        results_available = int(metadata.get("resultsAvailable", 0) or 0)
    except (TypeError, ValueError):
        return

    if results_available <= 0:
        return

    last_result = metadata.get("lastResult")
    if last_result is None:
        first_result = metadata.get("firstResult")
        results_returned = metadata.get("resultsReturned")
        try:
            if first_result is None or results_returned is None:
                return
            last_result = int(first_result) + int(results_returned) - 1
        except (TypeError, ValueError):
            return

    try:
        completed = max(0, int(last_result))
    except (TypeError, ValueError):
        return

    percent = int((min(completed, results_available) / results_available) * 100)
    log(f"PROGRESS:{percent}", log_callback)


def _resolve_log_path(log_file: str) -> Optional[str]:
    if not log_file:
        return None
    if os.path.isabs(log_file):
        return log_file
    base_dir = get_settings().log_output_dir
    if not base_dir:
        return log_file
    return os.path.join(base_dir, log_file)


def credentials_db_path() -> str:
    os.makedirs("db", exist_ok=True)
    return os.path.join("db", "credentials.db")


def ensure_account_binding(data_db_path: str, account_name: str) -> None:
    """Ensure a local data database is associated with a single account."""
    conn = connect_sqlite(data_db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS account_binding (
            account_name TEXT PRIMARY KEY
        )
        """
    )
    cur.execute("SELECT account_name FROM account_binding LIMIT 1")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO account_binding (account_name) VALUES (?)", (account_name,))
    else:
        bound = row[0]
        if bound != account_name:
            conn.close()
            raise ValueError(
                "brightpearl_data.db is bound to '{bound}', but you attempted to use account '{account}'.".format(
                    bound=bound, account=account_name
                )
            )
    conn.commit()
    conn.close()


def fetch_credentials(account_name: str) -> Credentials:
    """Fetch stored credentials for an account from SQLite."""
    conn = connect_sqlite(credentials_db_path())
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS credentials (
            account_name TEXT PRIMARY KEY,
            app_ref TEXT NOT NULL,
            token TEXT NOT NULL,
            region TEXT NOT NULL CHECK (region IN ('euw1', 'use1')),
            base_currency TEXT
        )
        """
    )
    cursor.execute(
        "SELECT app_ref, token, region FROM credentials WHERE account_name = ?",
        (account_name,),
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        raise ValueError(f"No credentials found for account '{account_name}'.")

    app_ref, token, region = result
    return Credentials(app_ref=app_ref, token=token, region=region)


def fetch_credentials_with_currency(account_name: str) -> Tuple[Credentials, Optional[str]]:
    """Fetch credentials plus any stored base currency for an account."""
    conn = connect_sqlite(credentials_db_path())
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS credentials (
            account_name TEXT PRIMARY KEY,
            app_ref TEXT NOT NULL,
            token TEXT NOT NULL,
            region TEXT NOT NULL CHECK (region IN ('euw1', 'use1')),
            base_currency TEXT
        )
        """
    )
    cursor.execute(
        "SELECT app_ref, token, region, base_currency FROM credentials WHERE account_name = ?",
        (account_name,),
    )
    result = cursor.fetchone()
    conn.close()
    if not result:
        raise ValueError(f"No credentials found for account '{account_name}'.")
    app_ref, token, region, base_currency = result
    return Credentials(app_ref=app_ref, token=token, region=region), base_currency


def update_base_currency(account_name: str, base_currency: str) -> None:
    """Persist the retrieved base currency for an account."""
    conn = connect_sqlite(credentials_db_path())
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE credentials SET base_currency = ?
        WHERE account_name = ?
        """,
        (base_currency, account_name),
    )
    conn.commit()
    conn.close()


def get_base_currency(
    api_url_config: str,
    headers: dict,
    log_callback: LogCallback = None,
    cancel_token=None,
) -> Optional[str]:
    """Fetch the Brightpearl base currency, respecting cancellation."""
    if cancel_token and cancel_token.is_set():
        return None
    try:
        resp = requests.get(api_url_config, headers=headers, verify=False, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["response"]["configuration"]["baseCurrencyCode"]
    except Exception as exc:  # broad exception is fine for logging here
        log(f"Error fetching baseCurrencyCode: {exc}", log_callback)
        return None


def sleep_with_cancel_ms(ms: int, cancel_token=None, log_callback: LogCallback = None) -> None:
    """Sleep for ``ms`` milliseconds, aborting early when cancelled."""
    if ms <= 0:
        return
    step = 100  # ms
    waited = 0
    while waited < ms:
        if cancel_token and cancel_token.is_set():
            log("Throttle sleep interrupted by cancel.", log_callback)
            return
        time.sleep(min(step, ms - waited) / 1000)
        waited += step


def should_pause_for_throttle(
    requests_remaining: Optional[int],
    next_throttle_period: int,
) -> bool:
    """Return whether we should pause based on Brightpearl throttle headers."""
    if next_throttle_period <= 0 or requests_remaining is None:
        return False
    return requests_remaining < get_settings().throttle_threshold


def send_request(
    url: str,
    headers: dict,
    cancel_token=None,
    log_callback: LogCallback = None,
) -> Tuple[Optional[str], int, int]:
    """Send a GET request to Brightpearl, handling throttling metadata."""
    if cancel_token and cancel_token.is_set():
        return None, 0, 0
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        body = response.text
        log_payload(f"GET {url} -> {response.status_code}\n{body}", log_callback)
        next_throttle_period = int(response.headers.get("brightpearl-next-throttle-period", 0))
        requests_remaining = int(response.headers.get("brightpearl-requests-remaining", 0))
        return body, next_throttle_period, requests_remaining
    except requests.RequestException as exc:
        log(f"Error sending request: {exc}", log_callback)
        return None, 0, 0
