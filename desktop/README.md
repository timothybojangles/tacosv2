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
- Synthetic local CSV import, DuckDB page preview, filter and sort for large-data
  prototype work.
- Remote stock-correction writes are disabled in the UI.
- Local-only update proof notes under `update-proof/`.

## Current verification

Run on Tim's Windows checkout on 2026-09-11:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests/test_migration_regressions.py
.\.venv\Scripts\python.exe -m pytest -q -m "not legacy_gap"
$env:PYTHONPATH = "desktop\engine"; .\.venv\Scripts\python.exe -m pytest -q desktop\engine\tests
Set-Location desktop; npm run build; Set-Location src-tauri; cargo check
Set-Location ..; npm run tauri:build
```

Observed results:

- Full legacy/migration suite: 38 passed, 2 xfailed.
- Migration regression file: 7 passed, 2 xfailed.
- Non-legacy-gap selection: 38 passed, 2 deselected.
- Worker tests: 5 passed.
- Frontend build: passed.
- Cargo check: passed.
- Tauri NSIS build: produced `TACOS Desktop_0.1.0_x64-setup.exe`.

Measured artifact sizes from this build:

- Release desktop executable: 8,905,728 bytes.
- Private worker bundle: 60,974,205 bytes.
- NSIS setup executable with offline WebView2 prerequisite: 236,259,790 bytes.

The 20k/200k/2M dataset performance gate still needs measured hardware results
before the prototype can be treated as accepted.
