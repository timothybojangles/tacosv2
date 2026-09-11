from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
MAX_PAGE_SIZE = 500


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Store:
    root: Path
    ledger: Path
    datasets: Path


cancelled: set[str] = set()
cancel_lock = threading.Lock()


def app_store() -> Store:
    configured = os.environ.get("TACOS_DESKTOP_DATA_DIR")
    if configured:
        root = Path(configured)
    else:
        root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "TACOSv2"
    datasets = root / "datasets"
    datasets.mkdir(parents=True, exist_ok=True)
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
    return Store(root=root, ledger=ledger, datasets=datasets)


def output(payload: dict[str, Any]) -> None:
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
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

