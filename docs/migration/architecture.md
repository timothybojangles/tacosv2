# Target architecture

See [accepted decisions](decisions.md) for confirmed installation and distribution constraints. This design is not implemented yet.

## Platform options

| Option | Fit | Decision |
|---|---|---|
| Tauri + React/TypeScript + Python worker | Modern visual workflow ecosystem, native desktop boundaries, selective Python reuse; three-language build is the main cost | Recommended, provided the packaging prototype passes |
| pywebview + React/TypeScript + Python | Fewer languages and a useful fallback; more updater/process lifecycle integration belongs to TACOS | Use if Tauri adds disproportionate complexity in the prototype |
| Electron + React | Mature web desktop model, but bundles Chromium/Node and does not inherently solve update weight | Lower preference for this requirement |
| Full Rust or C# rewrite | Can create a strong new core, but translates all existing Brightpearl behaviour and fixtures | Do not require a full language conversion without measured benefit |
| Continued CustomTkinter redesign | Least initial disruption | Can improve existing screens, but does not best serve the intended visual workflow workbench |

Tauri supports separately packaged executables, including a Python sidecar ([sidecar documentation](https://v2.tauri.app/develop/sidecar/)). Its Windows installer supports NSIS setup executables ([installer documentation](https://v2.tauri.app/distribute/windows-installer/)). pywebview provides a Python/native-webview alternative ([project documentation](https://pywebview.flowrl.com/)). Electron embeds Chromium and Node ([architecture rationale](https://www.electronjs.org/docs/latest/why-electron)).

Recommended dependency policy: Tauri under MIT/Apache-2.0; React and React Flow under MIT; TypeScript under Apache-2.0; Python under PSF; SQLite public domain; DuckDB under MIT; NSIS under its published free-use licensing. Pin exact versions, retain notices, and audit transitive packages before release. These are intended unrestricted local components, not free service tiers. React Flow Pro examples/support are excluded: the core library is MIT ([React Flow licensing](https://reactflow.dev/pro)). A custom grid using TanStack Table/Virtual is suitable; its table engine leaves rendering under our control ([Table documentation](https://tanstack.com/table/latest), [open-source policy](https://tanstack.com/ethos)). A maintained XLSX reader such as openpyxl is a candidate to pin and license-check during foundation work.

Windows WebView2 is a Microsoft runtime dependency, not an open-source component. It is compatible with the stated allowance for free functions, subject to internal distribution approval. Prefer an IT-managed installed runtime; provide an offline prerequisite option. Do not assume its presence. Microsoft documents both deployment options ([WebView2 distribution](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)). App-package signature verification is separate from Windows publisher trust; ask IT about its existing code-signing/allowlisting process rather than promising warning-free installation at zero certificate cost.

## 5. Target architecture and user experience

The desktop process owns the window, dialogs and worker lifetime. React displays local bundled assets only. The Rust bridge exposes narrowly defined commands; a supervised Python worker receives framed, versioned messages over private process pipes. No web server, listening network port or installed service is necessary. The UI never receives API tokens. Data messages contain page-sized results and identifiers rather than entire datasets.

Python owns endpoint adapters, validation, transformation, execution, reference synchronisation and database access. SQLite stores accounts without secrets, jobs, steps, attempts, error records and workflow definitions. DuckDB stores bulk snapshots and transformation results. One worker owns each writable analytical database; immutable dataset versions make cross-database publication recoverable. Write the dataset, commit it, then publish its identifier in the job ledger; reconcile incomplete publication on restart rather than pretending SQLite and DuckDB share a transaction.

DuckDB is embedded, portable and designed for analytical queries ([design and MIT licence](https://duckdb.org/why_duckdb)). This does not establish a measured speed improvement for TACOS. Apply memory limits, bounded result fetching and controlled concurrency; see [workload tuning](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads) and [concurrency](https://duckdb.org/docs/current/connect/concurrency). Disable runtime extension downloads and network-capable analytical extensions; ship any required extensions locally. Do not make MotherDuck or another remote service part of this design.

Primary navigation: Tasks, Data, Workflows, API Explorer, Job History, Settings. Common work starts from task cards such as Import stock or Update products. Advanced JSON and workflow authoring remain available without dominating everyday screens. Keep the active account visible, but bind a running job to its original account so switching screens cannot redirect it.

Every import follows Select source → Map fields → Validate and correct → Preview changes → Run → Review results. Offer saved mappings, worksheet selection, a searchable reference picker, cell-level errors, filter-to-errors, bulk fixes and an undoable local edit history. Preserve source text and row identity. Show proposed encoding/date/number interpretations; never silently change identifiers or money values. Use decimal arithmetic and endpoint-specific rounding.

The same operation definitions drive forms, required reference fetches, payload building and execution. Server-only rules, stale data and permissions can still cause rejection: never advertise 100% prevention. Instead make each rejection understandable without logs: source row, business identifier, field if known, endpoint, original error, explanation, suggested correction and retry eligibility. If Brightpearl does not identify a field, show the error at operation level rather than inventing a mapping.

Visual ETL nodes: local file, Brightpearl download, local dataset, select/map, filter, lookup/join, calculate, validate, preview, Brightpearl action and export. Start with acyclic graphs and bounded dataset iteration, not arbitrary loops or Python eval. React Flow draws the graph; TACOS must implement execution, type checking, preview, persistence and recovery. Provide an equivalent ordered-step editor for users who find a canvas confusing. Template export contains definitions, not credentials or embedded customer data by default.

## 6. Reliable execution

Use explicit operation states: pending, ready, running, succeeded, rejected, retryable failure, outcome unknown, cancelled. Parent workflows additionally show partial completion. Store request fingerprints, prepared payload version, account, source identity and attempt metadata before sending.

No automatic replay of a non-idempotent write after an ambiguous disconnect. Reconcile using an endpoint-supported identity or readback where possible; otherwise require review. Do not invent a Brightpearl idempotency header. Retry 429 with rate-limit guidance, and retry eligible transient failures with bounded backoff/jitter. Reject permanent validation failures immediately. Share throttling per account across all concurrent jobs; API limits remain the ceiling regardless of local programming language.

Checkpoint each business action separately: create address, create contact, create order, add line, record payment. Store returned IDs before advancing. Process related order rows as one business unit; do not submit partial orders simply because some lines validate. Independent items may run separately after the user reviews the selection. Capture individual batch outcomes, including 207 bodies where relevant.

Validation produces an immutable prepared operation. Editing data, changing mappings or refreshing reference versions invalidates affected prepared operations. Revalidation does not erase previous run history. Reference downloads remain marked incomplete until every required page succeeds. Local cancellation stops new work and records any in-flight outcome; it cannot undo a successful Brightpearl write.

## 7. Installation and updates

Build a per-user Windows installer where policy permits. Bundle a private Python runtime and all required dependencies so end users never run pip, npm or a terminal. Python documents an embeddable Windows distribution intended for application integration ([Python documentation](https://docs.python.org/3/using/windows.html#the-embeddable-package)). Keep runtime/dependency versions separate from business-code/rule versions.

Use a stable bootstrap executable and versioned application directories. A routine update package includes only changed components, a signed manifest, file hashes, base-version requirements, deletions and compatible runtime/schema versions. A local Update.exe verifies and stages the package, checks free space, waits for jobs to stop, backs up operational databases with proper database-aware procedures, switches versions and runs a health check. Failed activation restores the compatible previous application/data pair. A major runtime upgrade may still require a larger package.

Tauri's standard updater supports signatures, but component-level/delta updating is additional TACOS engineering, not an automatic Tauri feature ([updater documentation](https://v2.tauri.app/plugin/updater/)). Prove this in phase 1. A full installer remains the repair/fallback route. Package signing can use an organisation-controlled offline key; protect it outside the repository. This does not replace Authenticode publisher trust.

Distribution baseline: a user obtains the update file through an approved company channel, then runs it locally. If approved, SharePoint can distribute application binaries only; it is not an operational database or runtime dependency. No requirement for a paid hosting plan or GitHub Actions quota: builds must run on an existing Windows workstation. No app self-update network access is necessary for v1.

Measure installed size, clean-install download, routine Python-rule update, UI update, dependency upgrade and rollback separately. Provisional goal: a dependency-unchanged rule/code patch is under 5 MB. That is an acceptance target to verify, not an existing capability or guarantee.

