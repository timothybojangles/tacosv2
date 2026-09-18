# TACOS desktop prototype

Phase 1 is a local installed prototype. It preserves the legacy app and does
not perform production Brightpearl writes.

## Local build

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r desktop\engine\requirements-worker.in
Set-Location desktop
npm install
npm run build
npm run tauri:build
```

For normal development, use the Tauri debug shell with Vite hot reload:

```powershell
cd desktop
npm run tauri:dev
```

Debug builds launch `desktop/engine/worker.py` through the repository virtual
environment, so Python changes do not require PyInstaller or an installer
rebuild. Release builds use only the privately bundled worker.

`npm run tauri:build` regenerates the private `tacos-engine.exe` worker with
PyInstaller before building the Tauri app. Generated files under
`desktop/engine-dist`, `desktop/dist`, `desktop/src-tauri/target` and
`node_modules` are not committed.

## Prototype scope

- Tauri 2 desktop shell with React/TypeScript UI.
- Rust command bridge that validates method names, protocol version, request ID
  and message size before forwarding to the worker.
- Private worker process over stdin/stdout pipes; no listening HTTP server.
- Python worker with SQLite job ledger and DuckDB dataset files under local app
  storage.
- Account selector with account-bound local data stores.
- Brightpearl account registration with region, app ref and account token.
- Windows builds store credentials through Windows Credential Manager; tests use
  an isolated plaintext backend only when explicitly configured.
- Credential validation through `integration-service/account-configuration`.
- Inventory reference sync reusing the legacy read-only product, warehouse,
  location and price-list sync helpers with Brightpearl throttle handling.
- Native source-file selection for CSV/XLSX inventory files.
- Account-bound consolidated CSV exception reports containing every rejected
  inventory row, while the UI keeps a bounded preview.
- Inventory validation reusing the legacy enrichment path and staging into
  `validated_inventory`.
- Local validation offers an explicit option to allow blank or zero quantity and
  cost values. Blanks become zero when enabled; non-finite numbers remain invalid.
- Cost can be sourced from the import file or a synced price list belonging to
  the selected account. In price-list mode, file `costprice` is ignored; missing
  or blank reference values resolve to zero only when the blank/zero option is
  enabled. Malformed non-numeric prices always reject the row. Zero list values
  require the same option. A SKU absent from the product catalogue still rejects
  because its product ID cannot be used for a price-list lookup.
- Price-list workflow: select the account, sync inventory references, choose a
  cost source, then validate. Validation stores the resolved cost in
  `validated_inventory.costprice` for the accepted preview. Changing the source
  file or cost options clears the previous result. There is no separate Enhance
  action; the Brightpearl write path remains disabled.
- Run testing is a local dry run: the worker requires the latest successful
  account-bound validation and a base currency, then builds stock-correction
  payloads in warehouse batches using unprocessed validated rows. It writes all
  request bodies to a downloadable JSONL report and shows a bounded sample.
  The preview displays a SHA-256 digest; export rejects a report changed since
  preview. This identifies the local payload artifact, not a live-run approval.
  No API POST is made and no inventory row is marked processed. Refreshing
  references requires validation again before another dry run.
- Validation streams exception rows to disk, reports progress every 500 rows,
  and creates a SKU index to speed repeated catalogue lookups.
- Synthetic local CSV import, DuckDB page preview, filter and sort for large-data
  prototype work.
- Remote stock-correction writes are disabled in the UI.
- Local-only update proof notes under `update-proof/`.

## Current verification

Run on Tim's Windows checkout on 2026-09-18:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests/test_migration_regressions.py
.\.venv\Scripts\python.exe -m pytest -q -m "not legacy_gap"
$env:PYTHONPATH = "desktop\engine"; .\.venv\Scripts\python.exe -m pytest -q desktop\engine\tests
Set-Location desktop; npm run build; Set-Location src-tauri; cargo check
Set-Location ..; npm run tauri:build
```

Observed results:

- Full legacy/migration suite: 51 passed, 2 xfailed.
- Migration regression file: 20 passed, 2 xfailed.
- Non-legacy-gap selection: 51 passed, 2 deselected.
- Worker tests: 9 passed.
- Frontend build: passed.
- Cargo check: passed.
- Tauri NSIS build: produced `TACOS Desktop_0.1.0_x64-setup.exe`.

Measured artifact sizes from this build:

- Release desktop executable: 9,129,472 bytes.
- Private worker bundle: 63,379,218 bytes.
- NSIS setup executable with offline WebView2 prerequisite: 237,069,721 bytes.

The 20k/200k/2M dataset performance gate still needs measured hardware results
before the prototype can be treated as accepted.
