# Next Codex task: installed desktop prototype

Use this prompt after the baseline PR is merged or from a branch containing it. Do not execute against a different repository.

---

Work only in timothybojangles/tacosv2. Read repository instructions and docs/migration/{decisions,development,architecture,feature-matrix}.md. The user has approved migration, Windows per-user installation, manual download/run updates through SharePoint, and inventory import as the first pilot. IT supplies no code-signing certificate. Preserve the legacy app alongside the replacement. No production API operations.

First resolve the baseline gate: install development dependencies in an isolated environment, run existing tests and tests/test_migration_regressions.py, and commit platform-appropriate exact dependency locks. Investigate unexpected failures. The six legacy-gap cases should fail only for their named assertion; two positive inventory cases must pass. Do not remove strict xfails without implementing and verifying the corresponding correction. Record actual results.

Then implement the phase-1 installed prototype:

1. Add desktop/ with Tauri 2 and React/TypeScript and an engine/ package for a privately bundled Python worker. Rust only handles desktop integration, command validation, private process IPC and worker lifecycle; business rules remain Python. Node/Rust/Python tooling is for developers, never required on user machines.
2. Bundle all UI assets locally. No Firebase, Dropbox, hosted runtime, telemetry, CDN assets or paid/usage-tier dependencies. Use open-source components with licence notices. Keep runtime databases, logs, temp files and credentials outside OneDrive/SharePoint sync roots. SharePoint distributes application files only; no Graph integration is needed.
3. Implement a versioned private pipe protocol with typed requests/events, request IDs, bounded message sizes, cancellation, worker shutdown/restart and readable worker failure UI. Do not open a listening HTTP server. Never transfer tokens or millions of rows to the UI.
4. Add a local SQLite job ledger and single-owner DuckDB analytical store. Import synthetic local CSV, preview a page, filter/sort using the engine, and display progress. Ship required extensions; no runtime extension downloads. Use synthetic records only in this phase. Handle malformed CSV without discarding the source.
5. Create the inventory task's navigation and source/mapping/validation/result screen structure. Clearly mark unimplemented steps; disable remote writes. Do not present a simulated upload as a working Brightpearl integration.
6. Produce a per-user Windows setup executable bundling the private Python runtime and dependencies. Detect WebView2 and document the approved offline prerequisite option; do not assume it is installed. Do not bypass Windows security or imply that self-generated update signatures establish Windows publisher trust.
7. Prove a local small-update mechanism: versioned app components, stable bootstrapper, signed package manifest, hashes, explicit deletions, base/runtime/schema compatibility, staging and rollback. The private update signing key stays outside Git. Demonstrate a code-only change without redistributing unchanged runtime dependencies. Tauri's standard updater alone is not proof of delta/component updates. Retain a full installer repair path.
8. Verify 20k/200k/2M synthetic datasets on documented hardware: bounded worker/UI memory, paged data transfer and a responsive window. Record cold startup, first-page time, filter/join time, installed size, clean-install size, code-only patch size and runtime-change size. Treat under-5-MB code-only updates as a provisional goal, not a guaranteed result.
9. Test malformed IPC, unexpected worker exit, cancellation, wrong-base update, corrupt/tampered update, deleted files, interrupted installation, low disk space and recovery to a compatible prior version. Keep legacy working and preserve user data on uninstall unless explicitly requested.

Deliver a bounded draft PR with source, local build instructions and measured results. If the environment cannot build Windows installers or download required dependencies, report the exact blocked gates and provide commands for the user's Windows machine. Do not claim tests or installers succeeded without evidence. Do not silently substitute another desktop stack; document prototype evidence if recommending pywebview instead.

Stop at the prototype acceptance gate before porting all Brightpearl workflows. The next task will implement the shared execution engine and inventory pilot with verified endpoint contracts, structured row errors, complete reference snapshots and safe write reconciliation.
