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
- Account disconnect removes the saved credential and selector entry while
  preserving that account's local database and reports for safe reconnection.
- Brightpearl account registration with region, app ref and account token.
- Windows builds store credentials through Windows Credential Manager; tests use
  an isolated plaintext backend only when explicitly configured.
- Credential validation through `integration-service/account-configuration`.
- Inventory reference sync reusing the legacy read-only product, warehouse,
  location and price-list sync helpers with Brightpearl throttle handling.
- Product reference pages are staged in SQLite and checked against Brightpearl's
  `resultsAvailable` count before an atomic catalogue replacement. Read failures
  are retried using the configured backoff; exhausted retries fail the job and
  preserve the previous complete catalogue. The UI shows product count and
  percentage progress.
- Per-account data is stored at
  `%LOCALAPPDATA%\TACOSv2\accounts\<account>\brightpearl_data.sqlite`; the job
  ledger is `%LOCALAPPDATA%\TACOSv2\jobs.sqlite`. `reference_sync_progress` and,
  while a product sync is running or failed, `product_catalogue_sync` are safe
  read-only inspection points for SQLiteStudio or similar tools.
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
- Dry-run preview is local: the worker requires the latest successful
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
- Live stock corrections are available only after a current account-bound
  validation, reviewed dry run, and typed account-name confirmation. The UI
  submits every batch in order and can stop between batches. There is no
  arbitrary correction count cap. Brightpearl requires whole-number adjustment
  quantities; fractional quantities fail the dry run before any write. Values
  are additive stock adjustments, not target on-hand quantities.
- The worker sends only the documented `corrections` request body over verified
  HTTPS. Each batch is recorded as `sending` before POST. A confirmed response
  marks its source rows processed in the same local transaction as the batch
  success record. Timeouts, unexpected responses, or process death leave an
  uncertain batch that blocks further writes; it is never automatically retried.
  Reconcile that batch's warehouse, row count, and time against Brightpearl
  stock-correction notes before attempting any further run. Revalidating the
  same source can create new staged rows, so do not use it as a retry action.
  The app restores an unfinished run after restart and blocks credential edits,
  reference refreshes and revalidation while it remains open.
- This live path has been tested with mocked HTTP. The operator must choose and
  review a sandbox account/source in the desktop UI before a real sandbox run;
  no production account or live write was used during implementation.
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

Baseline results recorded before the live-run change:

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

Live-run build verification on 2026-09-18 used an isolated Python 3.13.3
environment because the existing `.venv` still points to an unavailable
Python 3.12 executable. This is a test build, not a refreshed Python 3.12 lock:

- `pytest -q tests desktop/engine/tests`: 64 passed, 2 strict xfailed.
- Frontend build and Cargo tests: passed (1 Rust test).
- Packaged worker CSV smoke test: passed.
- NSIS 0.1.1 installer: 237,362,391 bytes; SHA-256
  `19A9B4A4B9EB91690F066F3A5B0DE0B26AE0F478D10043F1D3C33E4554904909`.
- Sandbox live POST: not yet exercised; all new endpoint tests used mocked HTTP.

Reference-sync completeness build verification on 2026-09-21:

- Full Python suites: 65 passed, 1 strict xfailed (SO-001 only).
- Frontend build and Cargo tests: passed.
- Packaged worker CSV smoke test: passed.
- NSIS 0.1.2 installer: 237,468,739 bytes; SHA-256
  `4D79040F30ED8130565E10039CEB1B2338AFEE4B09C715C6B464DC319ECAACC6`.
- Authenticode status: unsigned, as expected without an IT-supplied signing certificate.

Progress-listener correction build on 2026-09-21:

- NSIS 0.1.3 installer: 237,468,901 bytes; SHA-256
  `30AB89027652897B9C1CB21E2601A6EE22D5BEA9B538A195E7B821FE8D83B2BF`.
- Frontend production build and Rust tests passed. The worker payload field is
  `event`; 0.1.2 incorrectly checked `name`, leaving visible progress at zero.
- In 0.1.4, blocking worker pipe I/O runs on Tauri's blocking task pool instead
  of the window thread, allowing Windows to repaint the app and deliver progress
  events throughout long reference syncs.
- NSIS 0.1.4 installer: 237,490,195 bytes; SHA-256
  `FB320063031779B0D861B80F639E08C27353A39865D0920D928D4EB77391B806`.

Direct progress-channel and account-disconnect build on 2026-09-21:

- Full Python suites: 66 passed, 1 strict xfailed (SO-001 only).
- Frontend production build and Rust tests passed (2 Rust tests).
- Packaged worker CSV smoke test passed.
- Long-running reference sync updates use a dedicated direct Tauri event channel.
- Account disconnect removes credentials and the selector entry while preserving
  local account data and reports.
- NSIS 0.1.5 installer: 237,487,362 bytes; SHA-256
  `C179D4021FDE2408429E3114B3F27DDAE9C6A880FC6A0CFF20A586108AFB9E3C`.
- Authenticode status: unsigned, as expected without an IT-supplied signing certificate.

The 20k/200k/2M dataset performance gate still needs measured hardware results
before the prototype can be treated as accepted.
