"""Utility helpers for the Shoot APIs tool."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .common import PROCESSED_AT_COLUMN, PROCESSED_COLUMN, PROCESSED_DETAILS_COLUMN, log_payload_exchange, quote_sql_identifier
from .settings import get_settings
from .throttle import parse_int_header


API_METHODS = ("GET", "POST", "PUT", "PATCH", "OPTIONS", "DELETE")
@dataclass(frozen=True)
class ApiResponse:
    status_code: Optional[int]
    response_text: str
    response_headers: str
    next_throttle: int
    remaining: Optional[int]
    error_detail: str = ""


def get_base_api_url(account_name: str, region: str) -> str:
    return f"https://{region}.brightpearlconnect.com/public-api/{account_name}/"


def normalize_api_url(raw_url: str, account_name: str, region: str) -> str:
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return ""
    base_url = get_base_api_url(account_name, region)
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(raw_url)
        path = parsed.path or ""
        marker = f"/public-api/{account_name}/"
        if marker in path:
            raw_url = path.split(marker, 1)[1]
        else:
            raw_url = path.lstrip("/")
    return f"{base_url}{raw_url.lstrip('/')}"


def replace_template_vars(text: str, variables: dict) -> str:
    if not text:
        return ""
    rendered = text
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def ensure_variables_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS variables (
            name TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS variables_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_response_mappings_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS response_variable_mappings (
            variable_name TEXT NOT NULL,
            loadout_name TEXT NOT NULL,
            json_path TEXT NOT NULL,
            PRIMARY KEY (variable_name, loadout_name)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_response_mappings_loadout "
        "ON response_variable_mappings (loadout_name)"
    )
    conn.commit()
    conn.close()


def save_response_mapping(db_path: str, variable_name: str, loadout_name: str, json_path: str) -> None:
    ensure_response_mappings_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO response_variable_mappings (variable_name, loadout_name, json_path)
        VALUES (?, ?, ?)
        ON CONFLICT(variable_name, loadout_name) DO UPDATE SET
            json_path = excluded.json_path
        """,
        (variable_name, loadout_name, json_path),
    )
    conn.commit()
    conn.close()


def delete_response_mapping(db_path: str, variable_name: str, loadout_name: str) -> None:
    ensure_response_mappings_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM response_variable_mappings
        WHERE variable_name = ? AND loadout_name = ?
        """,
        (variable_name, loadout_name),
    )
    conn.commit()
    conn.close()


def load_response_mappings_for_variable(db_path: str, variable_name: str) -> list[tuple[str, str]]:
    ensure_response_mappings_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT loadout_name, json_path
        FROM response_variable_mappings
        WHERE variable_name = ?
        ORDER BY loadout_name COLLATE NOCASE
        """,
        (variable_name,),
    )
    rows = cur.fetchall()
    conn.close()
    return [(row[0], row[1]) for row in rows]


def load_response_mappings_for_loadout(db_path: str, loadout_name: str) -> list[tuple[str, str]]:
    ensure_response_mappings_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT variable_name, json_path
        FROM response_variable_mappings
        WHERE loadout_name = ?
        ORDER BY variable_name COLLATE NOCASE
        """,
        (loadout_name,),
    )
    rows = cur.fetchall()
    conn.close()
    return [(row[0], row[1]) for row in rows]


def load_response_mapping_summary(db_path: str) -> dict[str, int]:
    ensure_response_mappings_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT variable_name, COUNT(loadout_name)
        FROM response_variable_mappings
        GROUP BY variable_name
        """
    )
    rows = {name: count for name, count in cur.fetchall()}
    conn.close()
    return rows


def format_json_path(parts: list[Any]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part or ""):
                path += f".{part}"
            else:
                path += f"[{json.dumps(part, ensure_ascii=False)}]"
    return path


def parse_json_path(path: str) -> list[Any]:
    if not path or not path.startswith("$"):
        raise ValueError("JSON path must start with $.")
    parts: list[Any] = []
    index = 1
    while index < len(path):
        if path[index] == ".":
            index += 1
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", path[index:])
            if not match:
                raise ValueError(f"Invalid JSON path segment near: {path[index:]}")
            parts.append(match.group(0))
            index += len(match.group(0))
        elif path[index] == "[":
            end = path.find("]", index)
            if end == -1:
                raise ValueError("Missing closing bracket in JSON path.")
            token = path[index + 1 : end]
            if token.startswith('"'):
                parts.append(json.loads(token))
            else:
                parts.append(int(token))
            index = end + 1
        else:
            raise ValueError(f"Invalid JSON path token near: {path[index:]}")
    return parts


def get_value_at_json_path(data: Any, json_path: str) -> Any:
    parts = parse_json_path(json_path)
    current = data
    for part in parts:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current) or part < 0:
                raise KeyError(f"Index {part} not found in JSON path.")
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise KeyError(f"Key '{part}' not found in JSON path.")
            current = current[part]
    return current


def _coerce_selection_value(selection_text: str) -> Any:
    trimmed = selection_text.strip()
    if not trimmed:
        raise ValueError("Selection is empty.")
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        return trimmed.strip('"')


def find_json_path_for_selection(response_text: str, selection_text: str) -> str | None:
    if not response_text or not selection_text:
        return None
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return None
    target = _coerce_selection_value(selection_text)

    def walk(value: Any, path: list[Any]) -> list[Any] | None:
        if value == target:
            return path
        if isinstance(value, dict):
            for key, child in value.items():
                found = walk(child, [*path, key])
                if found is not None:
                    return found
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                found = walk(child, [*path, idx])
                if found is not None:
                    return found
        return None

    found_path = walk(payload, [])
    if found_path is None:
        return None
    return format_json_path(found_path)

def ensure_loadouts_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS api_loadouts (
            quickname TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            payload TEXT NOT NULL,
            method TEXT NOT NULL
        )
        """
    )
    cur.execute("PRAGMA table_info(api_loadouts)")
    columns = {row[1] for row in cur.fetchall()}
    if "method" not in columns:
        cur.execute("ALTER TABLE api_loadouts ADD COLUMN method TEXT NOT NULL DEFAULT 'POST'")
    conn.commit()
    conn.close()


def ensure_chain_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS api_chains (
            quickname TEXT PRIMARY KEY
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS api_chain_loadouts (
            chain_name TEXT NOT NULL,
            loadout_name TEXT NOT NULL,
            run_order INTEGER NOT NULL,
            PRIMARY KEY (chain_name, loadout_name)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS api_chain_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_chain TEXT
        )
        """
    )
    cur.execute(
        "INSERT OR IGNORE INTO api_chain_state (id, active_chain) VALUES (1, NULL)"
    )
    conn.commit()
    conn.close()


def load_chain_names(db_path: str) -> list[str]:
    ensure_chain_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT quickname FROM api_chains ORDER BY quickname COLLATE NOCASE")
    rows = [row[0] for row in cur.fetchall()]
    conn.close()
    return rows


def load_chain_loadouts(db_path: str, chain_name: str) -> list[str]:
    ensure_chain_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT loadout_name
        FROM api_chain_loadouts
        WHERE chain_name = ?
        ORDER BY run_order ASC
        """,
        (chain_name,),
    )
    rows = [row[0] for row in cur.fetchall()]
    conn.close()
    return rows


def save_chain(db_path: str, chain_name: str, loadouts: list[str]) -> None:
    ensure_chain_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO api_chains (quickname) VALUES (?) ON CONFLICT(quickname) DO NOTHING",
        (chain_name,),
    )
    cur.execute("DELETE FROM api_chain_loadouts WHERE chain_name = ?", (chain_name,))
    for idx, loadout_name in enumerate(loadouts, start=1):
        cur.execute(
            """
            INSERT INTO api_chain_loadouts (chain_name, loadout_name, run_order)
            VALUES (?, ?, ?)
            """,
            (chain_name, loadout_name, idx),
        )
    conn.commit()
    conn.close()


def delete_chain(db_path: str, chain_name: str) -> None:
    ensure_chain_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM api_chain_loadouts WHERE chain_name = ?", (chain_name,))
    cur.execute("DELETE FROM api_chains WHERE quickname = ?", (chain_name,))
    cur.execute(
        "UPDATE api_chain_state SET active_chain = NULL WHERE active_chain = ?",
        (chain_name,),
    )
    conn.commit()
    conn.close()


def get_active_chain(db_path: str) -> str | None:
    ensure_chain_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT active_chain FROM api_chain_state WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_active_chain(db_path: str, chain_name: str | None) -> None:
    ensure_chain_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "UPDATE api_chain_state SET active_chain = ? WHERE id = 1",
        (chain_name,),
    )
    conn.commit()
    conn.close()


def load_api_loadouts(db_path: str) -> list[tuple[str, str, str]]:
    ensure_loadouts_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT quickname, method, url FROM api_loadouts ORDER BY quickname COLLATE NOCASE"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def fetch_api_loadout(db_path: str, quickname: str) -> tuple[str, str, str] | None:
    ensure_loadouts_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, payload, method FROM api_loadouts WHERE quickname = ?",
        (quickname,),
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return row[0], row[1], row[2]


def save_api_loadout(db_path: str, quickname: str, url: str, payload: str, method: str) -> None:
    ensure_loadouts_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO api_loadouts (quickname, url, payload, method)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(quickname) DO UPDATE SET
            url = excluded.url,
            payload = excluded.payload,
            method = excluded.method
        """,
        (quickname, url, payload, method),
    )
    conn.commit()
    conn.close()


def delete_api_loadout(db_path: str, quickname: str) -> None:
    ensure_loadouts_table(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM api_loadouts WHERE quickname = ?", (quickname,))
    conn.commit()
    conn.close()




def ensure_variables_data_tracking_columns(db_path: str) -> None:
    ensure_variables_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(variables_data)")
    existing = {row[1] for row in cur.fetchall()}
    for column in (PROCESSED_COLUMN, PROCESSED_AT_COLUMN, PROCESSED_DETAILS_COLUMN):
        if column not in existing:
            cur.execute(f"ALTER TABLE variables_data ADD COLUMN {quote_sql_identifier(column)} TEXT")
    conn.commit()
    conn.close()

def create_variables_data_table(db_path: str, headers: list[str]) -> None:
    ensure_variables_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS variables_data")
    if not headers:
        cur.execute("CREATE TABLE variables_data (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.commit()
        conn.close()
        return
    columns = ", ".join(f"{quote_sql_identifier(name)} TEXT" for name in headers)
    cur.execute(f"CREATE TABLE variables_data (id INTEGER PRIMARY KEY AUTOINCREMENT, {columns})")
    conn.commit()
    conn.close()


def load_variables(db_path: str) -> dict:
    ensure_variables_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name, value FROM variables ORDER BY name COLLATE NOCASE")
    rows = {name: value for name, value in cur.fetchall()}
    conn.close()
    return rows


def save_variable(db_path: str, name: str, value: str) -> None:
    ensure_variables_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO variables (name, value)
        VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
        """,
        (name, value),
    )
    conn.commit()
    conn.close()


def delete_variable(db_path: str, name: str) -> None:
    ensure_variables_tables(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM variables WHERE name = ?", (name,))
    conn.commit()
    conn.close()


def load_variables_data(db_path: str) -> list[dict]:
    ensure_variables_data_tracking_columns(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(variables_data)")
    columns = [row[1] for row in cur.fetchall() if row[1] != "id"]
    if not columns:
        conn.close()
        return []
    column_list = ", ".join(quote_sql_identifier(name) for name in columns)
    cur.execute(f"SELECT {column_list} FROM variables_data ORDER BY id")
    rows = []
    for values in cur.fetchall():
        rows.append(dict(zip(columns, values, strict=False)))
    conn.close()
    return rows


def load_variables_data_with_ids(db_path: str) -> list[dict]:
    ensure_variables_data_tracking_columns(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(variables_data)")
    columns = [row[1] for row in cur.fetchall()]
    if not columns:
        conn.close()
        return []
    column_list = ", ".join(quote_sql_identifier(name) for name in columns)
    cur.execute(f"SELECT {column_list} FROM variables_data ORDER BY id")
    rows = []
    for values in cur.fetchall():
        rows.append(dict(zip(columns, values, strict=False)))
    conn.close()
    return rows




def _find_first_message(value: Any) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("message"), str) and value.get("message").strip():
            return value.get("message").strip()
        for nested in value.values():
            found = _find_first_message(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_first_message(nested)
            if found:
                return found
    return None


def build_processed_details(status_code: int, response_text: str) -> str:
    details = f"Status {status_code}"
    message = None
    if response_text:
        try:
            payload = json.loads(response_text)
            message = _find_first_message(payload)
        except (TypeError, ValueError):
            message = None
    if message:
        details += f' | "message": "{message}"'
    return details

def is_row_processed(row: dict, *, treat_207_as_processed: bool = False) -> bool:
    raw_value = str(row.get(PROCESSED_COLUMN) or "").strip()
    if raw_value == "1":
        return True
    return treat_207_as_processed and raw_value == "2"


def mark_variables_row_processed(db_path: str, row_id: int, status_code: int, response_text: str = "") -> None:
    processed_value = None
    if status_code == 200:
        processed_value = "1"
    elif status_code == 207:
        processed_value = "2"
    if processed_value is None:
        return

    ensure_variables_data_tracking_columns(db_path)
    processed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    processed_details = build_processed_details(status_code, response_text)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        f"UPDATE variables_data "
        f"SET {quote_sql_identifier(PROCESSED_COLUMN)} = ?, "
        f"{quote_sql_identifier(PROCESSED_AT_COLUMN)} = ?, "
        f"{quote_sql_identifier(PROCESSED_DETAILS_COLUMN)} = ? "
        "WHERE id = ?",
        (processed_value, processed_at, processed_details, row_id),
    )
    conn.commit()
    conn.close()


def parse_json_payload(payload_text: str) -> tuple[Optional[Any], Optional[Exception]]:
    if not payload_text:
        return None, None
    try:
        return json.loads(payload_text), None
    except json.JSONDecodeError as exc:
        return None, exc


def build_response_text(response: requests.Response) -> str:
    response_text = response.text
    try:
        response_json = response.json()
        response_text = json.dumps(response_json, indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return response_text


def execute_api_request(
    method: str,
    url: str,
    headers: dict,
    json_payload: Optional[Any],
    *,
    timeout: int = 30,
) -> ApiResponse:
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=json_payload,
            verify=False,
            timeout=timeout,
        )
        log_payload_exchange(method, url, json_payload, response)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            pass

        response_text = build_response_text(response)
        response_headers = "\n".join(f"{key}: {value}" for key, value in response.headers.items())
        next_throttle = int(float(response.headers.get("brightpearl-next-throttle-period", 0) or 0))
        remaining = parse_int_header(response.headers, "brightpearl-requests-remaining", 2)
        return ApiResponse(
            status_code=response.status_code,
            response_text=response_text,
            response_headers=response_headers,
            next_throttle=next_throttle,
            remaining=remaining,
        )
    except Exception as exc:
        return ApiResponse(
            status_code=None,
            response_text=f"Request failed:\n{exc}",
            response_headers="",
            next_throttle=0,
            remaining=None,
            error_detail=str(exc),
        )


def throttle_sleep_ms(requests_remaining: Optional[int], throttle_ms: int) -> int:
    if requests_remaining is None:
        return 0
    threshold = get_settings().throttle_threshold
    if requests_remaining < threshold:
        return max(throttle_ms, 0)
    return 0


def is_success_status(status_code: int) -> bool:
    return 200 <= status_code < 300


def is_processed_success_status(status_code: int) -> bool:
    return status_code in {200, 207}


def insert_variables_rows(
    db_path: str,
    columns: Iterable[str],
    rows: Iterable[dict],
) -> None:
    column_list = ", ".join(quote_sql_identifier(name) for name in columns)
    placeholders = ", ".join("?" for _ in columns)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for row in rows:
        values = [row.get(name) for name in columns]
        cur.execute(f"INSERT INTO variables_data ({column_list}) VALUES ({placeholders})", values)
    conn.commit()
    conn.close()
