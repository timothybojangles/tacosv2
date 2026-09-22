# Feature parity baseline

Baseline commit: `74fdfccdf62653d18dd4f01f336343ad1309f27f`. All rows are pending replacement. See [source inventory](source-inventory.md) for every module and UI action. Schema/payload details remain in the linked modules and USER_GUIDE.md; this matrix is not a substitute for endpoint contract verification.



Every family below belongs in the parity checklist. Inclusion does not mean every existing behaviour is correct and should be copied.

| Existing capability | Relevant source | Migration treatment |
|---|---|---|
| Accounts, settings, progress and logs | main, common, paths, settings, performance | New local account onboarding; credentials in OS store; structured job history |
| Inventory import and valuation | validator, sync, inventory_import, inventory_pricelists | First complete pilot; validate warehouse/location/product relationships and numeric fields |
| Contacts and additional addresses | validator_contacts, sync_contacts, validator_addresses, sync_addresses, additional_addresses | Preserve field coverage; checkpoint address creation separately from contact/link creation |
| Product creation and update | product_import | Split schemas, references, validation, payloads and execution; retain variation/bundle/reference-creation capabilities |
| Custom fields for contacts/products/orders | custom_fields | Metadata-driven forms and typed values with cached definition versions |
| Warehouse locations and zones | warehouse_locations, warehouse_zones and wrappers | Guided create/update with relationship-aware validation |
| Open sales orders and payments | validator_sales_orders, sync_sales_orders | Parent/line validation and independent payment checkpoint; explicit ambiguous-outcome handling |
| Open purchases and payments | validator_open_purchases, sync_open_purchases | Same engine with purchase-specific rules and payload fixtures |
| Historic orders and rows | validator_historic_orders, sync_historic_orders | Checkpoint header and line operations; preserve complete-order grouping |
| Forget contacts and related orders | forget_contacts, forget_contact | Explicit target preview, irreversible-action confirmation and auditable outcomes |
| Warehouse service maintenance | warehouse_service_maintenance | Named operations, previews and checkpointed multi-step execution |
| Product catalogue export | product_catalogue_export | Streaming download/export, freshness status and dataset explorer |
| IP stock history | ip_stock_history | Local analytical workflow with warehouse/date filters |
| Large contact/order downloads | main and reference/catalogue helpers | Resumable pages, complete snapshots and deterministic pagination |
| API Shooter | shoot_api and main | Request explorer, typed payload builder, variables, response mapping, bulk runs and advanced JSON |
| Saved request chains | shoot_api and main | Visual ETL graphs and a simpler ordered-step editor using the same engine |
| Training helper | ic_training_helper | Preserve demo/reference/order/payment/stock helpers with clear account and action previews |
| CSV/XLSX and update tooling | csv_safety, launcher, bundle builder, spec | Replace readers and distribution architecture; retain compatible exports where useful |

## Current desktop parity status

The desktop application is an incremental replacement, not yet a whole-legacy
replacement. Missing families in the table above remain migration work and must
not be treated as intentionally removed functionality.

| Area | Desktop status | Remaining parity work |
|---|---|---|
| Accounts | Multiple account selector, OS credential storage, credential check, account-bound databases, safe disconnect with local data preservation | Account rename and explicit local-data purge policy |
| Inventory references | Products, warehouses, locations and price lists; complete product snapshot gate, retry/backoff, visible/persisted progress | User cancellation, per-reference retry controls, freshness/last-success display |
| Inventory source | Native CSV/XLSX picker, import-file or price-list cost, blank/zero policy | Downloadable source template and in-app column mapping |
| Inventory validation | Product/stock-tracked/location/warehouse/numeric validation, all row errors, previews and complete exception report | Editable correction workflow before revalidation |
| Inventory execution | Reviewed dry run, payload hash, typed account confirmation, throttled live batches, restart/resume and uncertain-outcome lock | Guided reconciliation action for uncertain batches |
| Logs/history | Local job history and persisted sync progress | Searchable detailed log viewer, export and retention controls |
| Other legacy modules | Not yet ported | Contacts, products, orders, purchases, locations/zones, exports, maintenance, API tools, training helpers and the remaining families above |

`update.py` is not an application updater: it contains contact catalogue code with unresolved names and inconsistent table/variable references. Determine whether anything still calls it before retiring it. Do not carry it into the new updater by filename association.


## Verification obligations

| ID | Current defect | Required result | Automated case |
|---|---|---|---|
| INV-001 | Numeric location lookup bypasses existence and warehouse | Reject unknown and other-warehouse IDs | two strict expected failures |
| INV-002 | Numeric quantity/cost not validated | Reject malformed numeric data before staging | two strict expected failures |
| SO-001 | Confirmed order re-created when retrying failed payment | Resume only incomplete payment | one strict expected failure |
| REF-001 | Incomplete refresh could replace complete references | Preserve previous complete snapshot | passing regression test |
| UPD-001 | Dropbox whole-EXE launcher and incomplete changed-file ZIP path | Offline authenticated component update; deletions, base-version checks, rollback | phase-1 Windows package test |

Endpoint request/response fixtures must be verified against current official Brightpearl documentation and the test account during each port. Existing behaviour is evidence of scope, not an oracle for correctness.

## Refactor Plan By Legacy Menu

The new app must preserve the legacy menu families. Do not collapse every tool into one generic module; shared infrastructure should sit underneath distinct task surfaces.

1. File, Settings and Help
   - Port Add account, Remove active account, Quit, Global Settings, Compact UI mode, About, app logo/theme behavior and progress/log panels.
   - Improve by using OS credential storage, account-bound data paths, clearer destructive-action prompts, searchable job history, readable error guidance and exportable logs.
   - Keep settings from `brightpearl/settings.py`: retry limits, log level, output folders, appearance, resolution and stock correction batch size.

2. Shared Platform Foundation
   - Build one operation model used by every module: source selection, mapping, validation, preview, run, checkpoint, resume/reconcile and review.
   - Keep reusable legacy helpers: `common`, `throttle`, `csv_safety`, `row_validation`, `reference_data`, `performance`, account binding and processing markers.
   - Standardize friendly errors: what happened, affected row/business id, why it matters, how to fix it, whether retry is safe.
   - Store jobs, steps, attempts, source identity, reference versions, payload hashes, output files and Brightpearl response summaries.

3. Configuration Tools
   - Contact Import: preserve contact catalogue sync, full contact validation, contact creation/update, price lists, tax/currency/staff-owner lookups and duplicate email checks.
   - Multiple Addresses: keep address CSV template, address validation, address creation/update and contact/address linking as a separate surface.
   - Product Import: preserve product templates, reference sync, supplier/contact catalogue, variants, bundles, brands/types/categories/options, missing reference creation, product creation and product id/SKU export.
   - Product Updates: preserve update picker, update template/config, update catalogue sync, sales channels, categories, variations, seasons, bundle components and update reference creation.
   - Custom Fields: preserve customer, supplier, product, sales and purchase custom-field metadata sync; CSV validation; typed value parsing; order catalogue sync; and Brightpearl patch execution.
   - Warehouse Locations: preserve warehouse sync, location catalogue sync, templates, all-location export, create validation/sync and update validation/sync.
   - Warehouse Zones: preserve zone catalogue sync, template, validation and create sync.

4. Go Live Tools
   - Inventory Import remains the reference implementation.
   - Open Sales: preserve order grouping, row validation, warehouse/status/country lookup, order creation, row posting, payment posting and failed-order CSV output.
   - Open Purchases: preserve purchase order grouping, row validation, warehouse/status lookup, order creation, row posting, purchase payment payloads and failed CSV output.
   - Historic Sales: preserve complete-order grouping, header/row validation, date parsing, warehouse/status/country lookup, order creation and row posting.
   - Improve all order tools with grouped previews, per-order checkpoints, payment as an independent step, no blind replay after uncertain writes and reconciliation prompts.

5. Maintenance Tools
   - Forget Contact (GDPR): preserve contact-id CSV import, forget-contact catalogue sync, contact forget execution, related order forget execution, failed CSVs and processed markers.
   - Warehouse Service Maintenance: preserve warehouse selector, task CSV import, product availability lookup, stock correction, correction note lookup, restore quantity logic, single/multi payload modes and progress.
   - Improve both with target previews, typed confirmation for irreversible actions, audit records and plain-language outcomes.

6. Export Tools
   - Product Catalogue Export: preserve reference sync, pricelist sync, supplier sync, product detail download, custom fields, reference-name resolution and CSV export.
   - IP Stock History: preserve audit CSV import, date parsing, warehouse filtering and per-warehouse export.
   - Improve with freshness indicators, filters, resumable downloads and saved export presets.

7. Experimental / Power Tools
   - SYNC: preserve aggregate sync actions from `run_all_syncs`, `run_inventory_import_syncs` and related reference refreshes, but rename into a clearer reference-data dashboard.
   - Shoot APIs: preserve URL normalization, variables, variables data table, JSON payload parsing, response mapping, saved loadouts, failed/run logs, 207 retry visibility, variable CSV import/run and chains.
   - IC Training Helper: preserve defaults check, dummy customer/product/shipping method, demo sales order/payment, quick stock, do-it-all, inventory reference values and random allocation.
   - Improve by separating production tools from training/demo tools and making chain execution inspectable before running.

8. Packaging and Update Tooling
   - Treat `launcher.py`, `build_update_bundle.py`, PyInstaller/Tauri packaging and installer generation as platform work, not business functionality.
   - Do not carry `update.py` forward as an updater; it is contact catalogue code with misleading filename and unresolved references.
   - Preserve CSV/XLSX compatibility and exported file formats where users depend on them.

9. Porting Order
   - Finish Inventory Import gaps first: templates, cancellation, freshness display and guided reconciliation.
   - Port Open Sales next because it has the known SO-001 retry/payment risk.
   - Then Open Purchases and Historic Sales using the same order engine.
   - Then Contact Import and Multiple Addresses.
   - Then Product Import/Updates and Custom Fields.
   - Then Warehouse Locations/Zones and Warehouse Service Maintenance.
   - Then Exports, API Shooter/Chains and IC Training Helper.

10. Definition of Done For Each Module
   - Legacy menu item exists under the same family in the new app.
   - Every legacy button/action/template/export has a new equivalent or a documented retirement decision.
   - Endpoint contracts checked against current Brightpearl docs before implementation.
   - Validation errors are row-specific and user-fixable.
   - Writes are checkpointed and safe against duplicate replay after uncertain outcomes.
   - Tests cover validation, payload shape, failure handling, progress and resume/reconcile behavior.
