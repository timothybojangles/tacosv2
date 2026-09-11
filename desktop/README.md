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
- Synthetic local CSV import, page preview, filter and sort.
- Inventory import navigation with source, mapping, validation, preview, run and
  result steps. Remote writes are disabled in the UI.
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

- Full legacy/migration suite: 33 passed, 6 xfailed.
- Migration regression file: 2 passed, 6 xfailed.
- Non-legacy-gap selection: 33 passed, 6 deselected.
- Worker tests: 1 passed.
- Frontend build: passed.
- Cargo check: passed.
- Tauri NSIS build: produced `TACOS Desktop_0.1.0_x64-setup.exe`.

Measured artifact sizes from this build:

- Release desktop executable: 8,329,216 bytes.
- Private worker bundle: 56,276,132 bytes.
- NSIS setup executable with offline WebView2 prerequisite: 232,958,164 bytes.

The 20k/200k/2M dataset performance gate still needs measured hardware results
before the prototype can be treated as accepted.
