# Accepted direction and delivery gates

Confirmed by Tim Olsen on 11 September 2026.

- Repository: timothybojangles/tacosv2 only.
- Windows is the priority; future macOS support is useful but not a release gate.
- Per-user installation is permitted. No end-user developer tools.
- Operational data, temporary data, logs and credentials remain local. Brightpearl API access is permitted.
- No Firebase, Dropbox, paid service tiers, subscriptions, hosted runtime or telemetry.
- SharePoint may distribute installer/update files for users to download and run. It must not hold the application's operational databases.
- IT does not supply application signing. Do not promise warning-free Windows installation, bypass security controls or purchase a certificate.
- A locally generated update verification key may authenticate our update packages; this is distinct from Windows publisher/Authenticode trust. Signing private keys must stay outside the repository.
- Inventory import is the first pilot. Windows hardware and Brightpearl test accounts are available from the user.
- Existing accounts do not need migration. Functionality stays Brightpearl-specific.
- Typical datasets: 20k–200k rows; prove the design with 2M synthetic records.

Recommended architecture: Tauri 2, React/TypeScript, a supervised Python worker, SQLite operational state and DuckDB analytical datasets. A small Rust bridge owns desktop integration and worker supervision. React Flow core can support later visual workflows without Pro dependencies. This recommendation remains subject to the installed prototype gate. pywebview is the fallback if Tauri's integration cost outweighs its benefits.

## Status

This change establishes the source baseline, regression obligations and implementation handoff. It does not deliver the desktop prototype, update.exe or a live inventory pilot. Legacy application behaviour is unchanged.

1. Baseline: feature matrix and known-gap tests authored; dependency inputs identified. Resolving/locking dependencies and executing pytest remain verification gates on a dependency-enabled machine.
2. Desktop prototype: build a Windows install, private worker IPC, paged local data browser, and authenticated small update/rollback proof. Measure installer, runtime and code-only patch sizes separately. No full feature port before this gate passes.
3. Engine and inventory pilot: typed validation, prepared operations, complete reference snapshots, per-record outcomes and safe replay/reconciliation.
4. Feature parity: port each remaining family in bounded PRs.
5. API Explorer and workflow canvas: both use the same operation engine as guided tasks.
6. Pilot release: validate on the user's Windows machine/test account before retiring the legacy interface.

No live credentials are required for stages 1–2. Do not store or include them in prompts, fixtures, templates or pull requests.
