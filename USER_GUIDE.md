# Multi-Tool (MT) User Guide

For the majority of the actions the MT is capable of we need to follow the same general steps:

We need to sync reference data from Brightpearl into the MT for the purpose of validation.

We need to feed data into the MT for the validation to take place before we

Sync validated data from the MT back into Brightpearl.

It is important to understand how the tool works to keep it running smoothly and avoid schoolboy errors:

The ‘Sync’ function typically takes a full sync of data related to the active module e.g. the Go Live Inventory Import sync will look up basic product information (SKU, productId, Stock Tracked status), all warehouses, warehouse locations and price list values for the entire catalogue. This allows the tool to validate that your inventory import file contains only SKUs which exist, no non-stock-tracked SKUs and is able to inform you if there are zero values against a product in the price list.

Performing the sync at the beginning of the task increases validation performance vs validating each line of * data updates * during the final sync e.g. the inventory import sync will process 50 updates at a time as long as they are validated. Without the initial sync Brightpearl would accept only a single update at a time, and even then it may fail if it was non-stock-tracked, non existent SKU etc.

If you make changes to data in Brightpearl after you have sync’d data into the MT you will need to resync that type of data again to ensure successful validation.

---

## Global prerequisites and behaviour

Before any module:
- Select the **correct Active account** in MT.
- Ensure account credentials are valid (App Ref, token, region).
- Run the module’s **reference sync** first.
- Use a CSV with **exact template header names**.
- Validate first; only then run final sync.
- If Brightpearl data changes during your work, re-sync and re-validate.

Case sensitivity and formatting principles used across MT:
- Header names are treated as exact schema labels; renaming headers can invalidate import.
- Lookup fields (e.g. SKU, order_ref, channel code) should be treated as exact values. Do not assume automatic case normalization.
- Dates must match the accepted pattern for that module.
- Numeric fields must be clean numeric values (no symbols, no text suffixes).

---

## Configuration tools

## 1) Contact imports

### 1. Steps to complete the action
1. Run contact reference sync (contact catalogue + tax/price list/currency refs).
2. Prepare CSV using the contact template headers.
3. Run contact validation.
4. Review invalid rows and fix source CSV.
5. Re-run validation until clean.
6. Sync validated contacts to Brightpearl.

### 2. Validation requirements
- Required headers must exist.
- `contact_catalogue` must exist (from sync).
- `ref_price_lists`, `ref_taxcode`, `ref_currencies` must exist.
- Numeric fields (credit/discount style fields) must parse where provided.
- Dates (if any in optional fields) must be parseable.
- Lookup values should match known reference values exactly.

### 3. What the final sync does
- Submits validated contact rows to Brightpearl create/update endpoints.
- Applies profile, communication, pricing and commercial attributes from CSV.
- Uses reference IDs resolved during validation where required.

### 4. Validation exception reports and troubleshooting
Typical messages:
- `CSV is missing required headers: ...`
  - Meaning: CSV schema mismatch.
  - Fix: regenerate from template, copy data columns back in.
- `contact_catalogue is missing. Sync Contact Refs first.`
  - Meaning: reference tables not loaded.
  - Fix: run sync and revalidate.
- `ref_price_lists/ref_taxcode/ref_currencies is missing`
  - Meaning: commercial reference data unavailable locally.
  - Fix: run Contact Reference sync.

### 5. Useful points
- If contacts were added directly in Brightpearl after your sync, re-sync before re-validation.
- Keep emails trimmed and consistent; whitespace often causes false mismatches.

---

## 2) Product imports and updates

### 1. Steps to complete the action
1. Sync product reference data.
2. Choose correct mode: **new product import** or **product update**.
3. Use corresponding template.
4. Run module validation.
5. Review missing-reference exports and fix.
6. Run final sync/create/update.

### 2. Validation requirements
- Template header set must match chosen mode.
- SKU/product references must exist for update mode.
- Mandatory creation fields must exist for import mode.
- Update fields must be in supported update list.
- Product-identifying fields should be exact-match values.

### 3. What the final sync does
- Import mode: creates products in Brightpearl.
- Update mode: applies field-level updates to existing products.
- Writes are limited to validated fields/rows.

### 4. Validation exception reports and troubleshooting
- Missing reference files indicate unresolved products/SKUs.
- Invalid update field errors indicate unsupported columns or typos.
- Any schema/header issue should be fixed by reusing MT template exactly.

### 5. Useful points
- Separate create and update jobs in distinct files to simplify rollback and troubleshooting.
- Re-sync before large updates if catalogue changed during prep.

---

## 3) Custom field imports

### 1. Steps to complete the action
1. Sync custom field definitions for the relevant entity (contact/product/order).
2. Sync target catalogue (entities to be updated).
3. Import custom field CSV.
4. Validate.
5. Correct invalid rows.
6. Sync valid data.

### 2. Validation requirements
- Custom field key must exist in synced definitions.
- Entity reference must resolve (correct contact/product/order).
- Value must conform to field type/accepted format.
- Field names and keys should be exact and consistently cased.

### 3. What the final sync does
- Sends custom field value updates to Brightpearl for each validated entity row.
- Keeps invalid rows out of writeback.

### 4. Validation exception reports and troubleshooting
- Unknown custom field key → resync definitions or correct key typo.
- Entity not found → resync catalogue or correct identifier.
- Type/value mismatch → correct value format (number/date/list option).

### 5. Useful points
- Keep one entity type per file; avoid mixed custom-field payloads.
- For list/dropdown style fields, standardize values directly from synced refs.

---

## 4) Additional contact addresses

### 1. Steps to complete the action
1. Sync contact catalogue.
2. Choose lookup mode: `contactId` or `primaryEmail`.
3. Prepare address CSV.
4. Validate addresses.
5. Correct unmatched entries.
6. Sync addresses.

### 2. Validation requirements
- Contact lookup value must resolve for each row.
- If using `contactId`, ID must parse as integer.
- Address structure fields must be present as required by endpoint.
- Lookup value matching should be treated as exact.

### 3. What the final sync does
- Creates additional postal addresses in Brightpearl and links them to contacts.
- Logs rows where API returns success but no postalAddressId.

### 4. Validation exception reports and troubleshooting
- Unmatched CSV output:
  - Meaning: no contact found for supplied lookup key.
  - Fix: check selected lookup mode; verify contact exists in synced catalogue.
- Numeric parse errors (ID mode):
  - Meaning: non-integer `contactId`.
  - Fix: clean field to integer only.

### 5. Useful points
- Email mode is safer where contact IDs are not known by source system.
- If many unmatched rows appear, refresh contact sync immediately.

---

## 5) Warehouse location imports and updates

### 1. Steps to complete the action
1. Sync warehouse + zone/location reference data.
2. Use location template.
3. Validate create/update file.
4. Correct rejected rows.
5. Run location sync.

### 2. Validation requirements
- Required headers must match template.
- Warehouse references must exist.
- Zone/location relationships must be valid.
- Required location identifiers/codes must be present and unique where required.

### 3. What the final sync does
- Creates new warehouse locations or updates existing location attributes.
- Applies changes only for validated rows.

### 4. Validation exception reports and troubleshooting
- Warehouse/zone not found:
  - Meaning: reference mismatch or stale sync.
  - Fix: resync refs and verify naming/codes.
- Duplicate/conflicting codes:
  - Meaning: same location code appears multiple times or collides with existing record.
  - Fix: deduplicate source and retry.

### 5. Useful points
- Keep zone creation/update cycle separate from location updates for clarity.

---

## 6) Warehouse zone imports

### 1. Steps to complete the action
1. Sync warehouse refs.
2. Prepare zone CSV using template.
3. Validate zones.
4. Correct invalid rows.
5. Sync zones.

### 2. Validation requirements
- Warehouse exists and is resolvable.
- Zone fields are present and valid.
- Zone keys/codes do not duplicate within warehouse context.

### 3. What the final sync does
- Creates or updates warehouse zones in Brightpearl.
- Makes zones available for downstream location imports.

### 4. Validation exception reports and troubleshooting
- Warehouse not found → stale refs or wrong warehouse identifier.
- Duplicate zone codes → source duplication or existing-zone conflict.

### 5. Useful points
- Perform zone sync before location sync to avoid dependent failures.

---

## Go live tools

## 1) Inventory imports

### 1. Steps to complete the action
1. Run Go Live inventory sync (products, stock tracked state, warehouses, locations, price refs).
2. Import inventory CSV.
3. Run validation/enrichment.
4. Review validation failures and correct data.
5. Choose cost behaviour (CSV price vs price list value).
6. Run final inventory sync.

### 2. Validation requirements
- SKU exists in synced product catalogue.
- SKU is stock tracked.
- Warehouse exists.
- Warehouse location exists and belongs to warehouse where required.
- Quantity parses as numeric/integer according to module logic.
- Price/cost values parse if required.
- Case/spacing should match synced identifiers exactly.

### 3. What the final sync does
- Posts validated stock updates to Brightpearl in batches.
- Increases/sets stock at the specified **warehouse location**.
- Applies stock cost from CSV unless user option is set to use synced price list values.
- Invalid/unvalidated lines are excluded from writeback.

### 4. Validation exception reports and troubleshooting
Common failure themes:
- SKU not found:
  - Meaning: SKU absent from synced catalogue.
  - Fix: verify SKU and rerun product sync.
- SKU non-stock-tracked:
  - Meaning: Brightpearl product cannot receive stock updates.
  - Fix: change product configuration or remove line.
- Warehouse/location mismatch:
  - Meaning: location does not exist in selected warehouse context.
  - Fix: correct location code or re-sync location refs.
- Price list zero/missing warning:
  - Meaning: chosen price source has no usable value.
  - Fix: populate value in BP or provide explicit CSV cost.

### 5. Useful points
- Initial sync significantly improves throughput (batch updates instead of one-by-one fallbacks).
- Re-sync immediately if stock-tracked status or locations changed in Brightpearl.

---

## 2) Open sales order imports

### 1. Steps to complete the action
1. Sync contacts/products/orders reference data.
2. Prepare CSV in sales order template structure.
3. Validate open sales orders.
4. Review generated invalid CSVs by category.
5. Correct source file and revalidate.
6. Run final open sales order sync.

### 2. Validation requirements
- `order_ref` present per order group.
- Customer email resolves in contact catalogue.
- Channel, price list, warehouse, currency, shipping method valid (shipping may be optional but validated when present).
- Order status valid for sales orders.
- `placed_on`, `tax_date`, `delivery_date` parse when provided.
- Payment method/date/amount valid if provided.
- Item SKU values resolve.
- Field values should be treated as exact/consistent case.

### 3. What the final sync does
- Creates open sales orders in Brightpearl.
- Creates order rows for each validated line.
- Applies payment details where included and valid.
- Skips rejected orders/rows from failed validation groups.

### 4. Validation exception reports and troubleshooting
Error files and meaning:
- `so_missing_customers.csv`: customer email not in contact catalogue.
- `so_invalid_channels.csv`: channel code/name not found.
- `so_invalid_price_lists.csv`: price list not found.
- `so_invalid_warehouses.csv`: warehouse not found.
- `so_invalid_currencies.csv`: currency not found in refs.
- `so_invalid_shipping_methods.csv`: shipping method supplied but not recognized.
- `so_invalid_statuses.csv`: status invalid for SO import.
- `so_invalid_payment_methods.csv`: payment method code invalid.
- `so_invalid_payment_dates.csv`: payment date format invalid.
- `so_invalid_payment_amounts.csv`: amount non-numeric.
- `so_invalid_tax_dates.csv`: tax_date invalid.
- `so_invalid_delivery_dates.csv`: delivery_date invalid.
- `so_invalid_placed_on.csv`: placed_on invalid.
- `so_processing_errors.csv`: unexpected exception while validating/order assembly.

Troubleshooting approach:
1. Fix highest-volume file first (usually reference mismatches).
2. Re-sync refs if errors are unexpectedly widespread.
3. Revalidate after each correction pass.

### 5. Useful points
- Keep one order per group with consistent repeated order-level fields.
- If optional payment fields are partially filled, complete all payment fields or clear them fully.

---

## 3) Open purchase order imports

### 1. Steps to complete the action
1. Sync product/warehouse/channel/status/currency refs used by PO validation.
2. Load PO CSV.
3. Validate open purchases.
4. Review category-specific invalid files.
5. Correct and revalidate.
6. Sync validated POs.

### 2. Validation requirements
- Required order-level fields exist.
- Required row-level fields exist.
- Warehouse/status/currency/channel/price list/shipping method references resolve.
- `placed_on` date must be valid (`YYYY-MM-DD` or `dd/mm/yyyy`).
- SKU must match product catalogue.
- Row quantity/value fields must be valid numeric forms.
- Quantity must be whole-number safe where strict integer enforced.

### 3. What the final sync does
- Creates purchase order header records.
- Creates PO lines for validated rows.
- Attempts payments only where method/date requirements are met.
- Skips rows/orders rejected during validation.

### 4. Validation exception reports and troubleshooting
- `purchase_missing_required_order.csv`: header-level mandatory fields missing.
- `purchase_missing_required_row.csv`: row-level mandatory fields missing.
- `purchase_invalid_channels.csv`: invalid channel reference.
- `purchase_invalid_price_lists.csv`: invalid price list reference.
- `purchase_invalid_warehouses.csv`: warehouse mismatch.
- `purchase_invalid_statuses.csv`: status invalid for PO.
- `purchase_invalid_shipping_methods.csv`: invalid shipping method.
- `purchase_invalid_currencies.csv`: invalid currency.
- `purchase_unmatched_skus.csv`: one or more item SKUs unresolved.
- `purchase_too_many_rows.csv`: order breached row constraints.

Interpretation tips:
- If one order appears in multiple error files, resolve all order-level reference issues before row-level fixes.
- Date-format errors often cascade; fix date columns early.

### 5. Useful points
- Use consistent supplier/order metadata across all lines for the same `order_ref`.
- Avoid decimal quantities when integer quantity is expected.

---

## 4) Historic order imports

### 1. Steps to complete the action
1. Sync all historic order dependencies (products/warehouses/channels/currencies etc.).
2. Import historic orders CSV.
3. Validate historic orders.
4. Work through invalid CSV sets.
5. Revalidate corrected file.
6. Run final historic order sync.

### 2. Validation requirements
- Required order and row fields present.
- Warehouse/status/channel/price list/currency/shipping refs valid.
- SKU matches synced product refs.
- Date fields valid per accepted format.
- Numeric values valid and row constraints met.

### 3. What the final sync does
- Creates historic order records and rows in Brightpearl according to mapped status/history rules.
- Excludes rejected rows and orders.

### 4. Validation exception reports and troubleshooting
- `historic_missing_required_order.csv`
- `historic_missing_required_row.csv`
- `historic_invalid_channels.csv`
- `historic_invalid_price_lists.csv`
- `historic_invalid_warehouses.csv`
- `historic_invalid_statuses.csv`
- `historic_invalid_shipping_methods.csv`
- `historic_invalid_currencies.csv`
- `historic_unmatched_skus.csv`
- `historic_too_many_rows.csv`

How to troubleshoot:
- Resolve reference-set errors first (warehouse/channel/currency).
- Then resolve SKU and row structure errors.
- Finally fix dates and optional field cleanliness.

### 5. Useful points
- Historic imports are best run in controlled batches with clear date-range segmentation.

---

## Maintenance tools

## 1) Forget contact in bulk (GDPR compliance)

### 1. Steps to complete the action
1. Prepare approved forget list.
2. Validate legal approval and retention policy.
3. Run bulk forget action.
4. Review success/failure logs.
5. Re-run only failed entries after diagnosis.

### 2. Validation requirements
- Contact identifiers must exist and resolve.
- Account credentials must permit forget/anonymize actions.
- Inputs should be clean (no malformed IDs/emails).

### 3. What the final sync does
- Executes forget/anonymization calls in Brightpearl for targeted contacts.
- Optional related order handling executes where function selected.

### 4. Validation exception reports and troubleshooting
- Not found errors: identifier mismatch or already forgotten.
- Permission errors: token/account lacks required access.
- Data-state conflicts: contact linked objects block operation path.

### 5. Useful points
- Always archive execution logs for compliance evidence.
- Run in smaller batches to simplify rollback investigations.

---

## 2) Warehouse service database maintenance

### 1. Steps to complete the action
1. Select warehouse service operation.
2. Import service task CSV.
3. Validate imported tasks.
4. Process tasks.
5. Review processing logs and retry failures.

### 2. Validation requirements
- Task CSV schema must match selected service operation.
- Warehouse/task references must resolve.
- Payload attributes must satisfy expected types/required fields.

### 3. What the final sync does
- Executes maintenance/service payloads against Brightpearl warehouse service endpoints.
- Marks/records task processing outcomes.

### 4. Validation exception reports and troubleshooting
- Header mismatch: wrong template for selected service.
- Invalid warehouse reference: stale refs or identifier typo.
- Payload conflict: incompatible values for selected operation.

### 5. Useful points
- Keep each run scoped to one service type for cleaner auditing.

---

## Export tools

## 1) Full product catalogue exports

### 1. Steps to complete the action
1. Sync product catalogue export data.
2. Optionally sync inventory price lists.
3. Optionally sync supplier data.
4. Choose export destination and run export.

### 2. Validation requirements
- Active account selected.
- Synced catalogue exists.
- Optional enrichment sync completed for extra columns.

### 3. What the final sync does
- Pulls/exports product catalogue records from local synced dataset to CSV.
- Includes optional price list and supplier columns if enabled and available.

### 4. Validation exception reports and troubleshooting
- Sync failure before export: rerun product export sync and check credentials.
- Missing optional columns: optional sync not run or no source data.

### 5. Useful points
- Use dedicated export filenames with timestamp and account name.

---

## 2) IP Stock History

### 1. Steps to complete the action
1. For import: select audit trail CSV and run import.
2. For export: choose warehouse scope and run export.
3. Review generated file list/logs.

### 2. Validation requirements
- Audit CSV must match expected schema.
- Warehouse identifiers must be valid for export.
- Date/value fields in audit trail must parse correctly.

### 3. What the final sync does
- Import mode: ingests stock-history audit CSV into MT process flow.
- Export mode: retrieves and writes IP stock history CSV files by warehouse.

### 4. Validation exception reports and troubleshooting
- Import failed: usually schema mismatch or malformed data types.
- Export failed: warehouse not resolvable or API retrieval issue.

### 5. Useful points
- Keep raw audit import files unchanged and archived for traceability.

---

## Experimental functions

## 1) API shooter (Brightpearl-specific Postman replacement)

### 1. Steps to complete the action
1. Build/load API loadout.
2. Set method + normalized URL.
3. Provide JSON payload/template variables where relevant.
4. Execute single request or chain.
5. Map response values if required.
6. Review processed/failed logs and retry.

### 2. Validation requirements
- HTTP method must be valid.
- URL must be syntactically valid.
- JSON payload must parse if JSON mode used.
- Template variables must resolve.
- JSON path mappings must be valid and reachable.

### 3. What the final sync does
- Sends request(s) directly to Brightpearl API using stored credentials.
- Supports row-based processing, processed-state marking, and chain execution.

### 4. Validation exception reports and troubleshooting
- 4xx responses: payload/schema/auth/permission issue.
- 5xx responses: remote server/transient issue; retry with throttle.
- Mapping failures: JSON path not present in actual response structure.

### 5. Useful points
- Start with one-row test data before chain-running large variable tables.
- Use throttling controls to avoid rate-limit spikes.

---

## 2) Large dataset syncing for contacts and orders

### 1. Steps to complete the action
1. Choose target dataset and append/first-result behaviour.
2. Run large sync in controlled window.
3. Monitor progress/logs.
4. Re-run with append settings as required.
5. Validate dependent imports against refreshed data.

### 2. Validation requirements
- Correct pagination controls and continuation options.
- Stable account/session credentials.
- Sufficient local storage for large reference datasets.

### 3. What the final sync does
- Pulls large contact/order datasets into local MT tables for downstream validation.
- Supports incremental append patterns where configured.

### 4. Validation exception reports and troubleshooting
- Partial dataset symptoms: pagination/window interrupted.
- Duplicate-looking entries: append mode used without dedupe strategy.
- Stale data warnings during downstream validation: re-sync before final writeback.

### 5. Useful points
- Run during low-traffic periods and avoid simultaneous high-volume module operations.

---

## Spreadsheet import and export expectations (all modules)

- Imports accept `.csv` and `.xlsx` files with a single header row. For XLSX,
  data is read from the first worksheet.
- Templates and interactive exports can be saved as either UTF-8 `.csv` or
  Excel `.xlsx`; choose the required format in the Save dialog.
- Keep the exact template header names regardless of the selected format.
- Do not alter template header names.
- Keep all mandatory columns populated.
- Keep identifiers trimmed and consistently formatted.
- Use module-approved date formats only.
- Keep numeric fields numeric (no currency symbols/commas unless module explicitly supports).
- Avoid hidden characters from spreadsheet exports.
- Save a clean copy before each validation iteration.

---

## Reading exception outputs quickly (recommended method)

1. Group error files into **reference errors**, **format errors**, **row-structure errors**.
2. Fix reference errors first (sync/data mapping issues).
3. Fix date/number formatting second.
4. Fix residual row-level data issues.
5. Revalidate and repeat until no exception files are produced.

---

## Operational best practices

- Always run a fresh sync for the module immediately before validation.
- Never mix unrelated module payloads in one CSV.
- Keep run logs, source files, and corrected files together for auditability.
- For go-live imports, perform a small pilot batch before full-scale run.
