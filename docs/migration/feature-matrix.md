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

`update.py` is not an application updater: it contains contact catalogue code with unresolved names and inconsistent table/variable references. Determine whether anything still calls it before retiring it. Do not carry it into the new updater by filename association.


## Verification obligations

| ID | Current defect | Required result | Automated case |
|---|---|---|---|
| INV-001 | Numeric location lookup bypasses existence and warehouse | Reject unknown and other-warehouse IDs | two strict expected failures |
| INV-002 | Numeric quantity/cost not validated | Reject malformed numeric data before staging | two strict expected failures |
| SO-001 | Confirmed order re-created when retrying failed payment | Resume only incomplete payment | one strict expected failure |
| REF-001 | Incomplete refresh can replace complete references | Preserve previous complete snapshot | one strict expected failure |
| UPD-001 | Dropbox whole-EXE launcher and incomplete changed-file ZIP path | Offline authenticated component update; deletions, base-version checks, rollback | phase-1 Windows package test |

Endpoint request/response fixtures must be verified against current official Brightpearl documentation and the test account during each port. Existing behaviour is evidence of scope, not an oracle for correctness.
