import tkinter as tk
from tkinter import messagebox, filedialog, END, VERTICAL, ttk
import customtkinter as ctk
from PIL import Image
import sqlite3
import os
import threading
import csv
import sys
import requests
import tkinter.font as tkfont
import json
import time
import re
import webbrowser
from pathlib import Path
from csv_safety import CSVEncodingError, check_csv_encoding, open_csv, open_table, repair_csv_encoding

# Import utility functions
from brightpearl.inventory_import import update_product_catalogue
from brightpearl.additional_addresses import update_contact_catalogue
from brightpearl.forget_contact import update_forget_contacts_catalogue
from brightpearl.product_import import (
    TEMPLATE_HEADERS as PRODUCT_IMPORT_TEMPLATE_HEADERS,
    PRODUCT_UPDATE_FIELDS,
    create_missing_product_update_references,
    create_missing_product_references,
    create_products_from_import,
    create_product_update_template,
    export_product_id_sku_reference,
    sync_product_reference_data,
    sync_product_updates,
    validate_product_import_csv,
    validate_product_update_csv,
)
from brightpearl.common import (
    fetch_credentials,
    log_payload,
    sleep_with_cancel_ms,
)
from brightpearl.custom_fields import (
    sync_contact_custom_fields,
    sync_custom_fields_to_brightpearl,
    sync_order_custom_fields,
    sync_order_catalogue,
    sync_product_custom_fields,
    validate_product_custom_fields_from_import_csv,
    validate_custom_fields_csv,
)
from brightpearl.product_catalogue_export import (
    export_synced_product_catalogue_to_csv,
    sync_export_product_suppliers,
    sync_product_catalogue_export_data,
)
from brightpearl.ip_stock_history import (
    export_ip_stock_history_by_warehouse,
    import_ip_stock_history_audit_csv,
)
from brightpearl.ic_training_helper import (
    check_defaults as ic_check_defaults,
    create_dummy_customer,
    create_dummy_product,
    create_dummy_shipping_method,
    create_sales_order,
    do_it_all as ic_do_it_all,
    allocate_random_inventory,
    inventory_reference_values,
    quick_stock,
    training_reference_values,
)
from brightpearl.paths import (
    get_credentials_db_path,
    get_data_db_path,
)
from brightpearl.settings import AppSettings, get_settings, normalize_settings, save_settings
from brightpearl.shoot_api import (
    API_METHODS as SHOOT_API_METHODS,
    create_variables_data_table,
    delete_api_loadout,
    delete_chain,
    delete_variable,
    delete_response_mapping,
    ensure_chain_tables,
    ensure_loadouts_table,
    ensure_response_mappings_table,
    ensure_variables_tables,
    execute_api_request,
    fetch_api_loadout,
    find_json_path_for_selection,
    get_value_at_json_path,
    insert_variables_rows,
    is_processed_success_status,
    is_row_processed,
    load_api_loadouts,
    load_chain_loadouts,
    load_chain_names,
    load_response_mapping_summary,
    load_response_mappings_for_loadout,
    load_response_mappings_for_variable,
    load_variables,
    load_variables_data,
    load_variables_data_with_ids,
    mark_variables_row_processed,
    normalize_api_url,
    parse_json_payload,
    PROCESSED_AT_COLUMN,
    PROCESSED_COLUMN,
    PROCESSED_DETAILS_COLUMN,
    parse_json_path,
    replace_template_vars,
    save_api_loadout,
    save_chain,
    save_response_mapping,
    save_variable,
    set_active_chain,
    get_active_chain,
    throttle_sleep_ms,
)
from brightpearl.performance import set_performance_callback
from brightpearl.warehouse_locations import (
    TEMPLATE_HEADERS as WAREHOUSE_LOCATION_TEMPLATE_HEADERS,
    export_locations_csv,
    sync_locations as sync_warehouse_locations,
    sync_location_updates,
    update_location_catalogue,
    validate_location_updates,
    validate_locations as validate_warehouse_locations,
)
from brightpearl.warehouse_zones import (
    TEMPLATE_HEADERS as WAREHOUSE_ZONE_TEMPLATE_HEADERS,
    sync_zones as sync_warehouse_zones,
    update_zone_catalogue,
    validate_zones as validate_warehouse_zones,
)
from brightpearl.warehouse_service_maintenance import (
    get_warehouse_options as get_warehouse_service_options,
    import_csv_tasks as import_warehouse_service_csv_tasks,
    process_tasks as process_warehouse_service_tasks,
)
from validator import validate_and_enrich_inventory
from validator_addresses import validate_addresses
from validator_contacts import validate_contacts
from validator_historic_orders import (
    TEMPLATE_HEADERS as HISTORIC_ORDER_TEMPLATE_HEADERS,
    validate_historic_orders,
)
from validator_open_purchases import (
    TEMPLATE_HEADERS as OPEN_PURCHASE_TEMPLATE_HEADERS,
    validate_open_purchases,
)
from validator_sales_orders import (
    TEMPLATE_HEADERS as SALES_ORDER_TEMPLATE_HEADERS,
    validate_sales_orders,
)
from sync import main as sync_inventory
from brightpearl.inventory_pricelists import (
    get_ref_price_lists,
    set_inventory_costs_from_pricelist,
    sync_inventory_pricelists,
)
from sync_addresses import main as sync_addresses
from sync_contacts import main as sync_contacts
from sync_historic_orders import main as sync_historic_orders
from sync_open_purchases import main as sync_open_purchases
from sync_sales_orders import main as sync_sales_orders
from forget_contacts import (
    run_forget_contacts as forget_contacts_worker,
    run_forget_contact_orders as forget_contact_orders_worker,
)
from reference_data import fetch_and_store_reference_tables

# ---------------- Globals / State ----------------
StringVar = tk.StringVar

APP_VERSION = "1.3.007"


def select_csv_for_validation(title="Select CSV file"):
    """Select a CSV and offer a loss-explicit UTF-8 repair when necessary."""
    csv_path = filedialog.askopenfilename(title=title, filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")])
    if not csv_path:
        return ""
    try:
        check_csv_encoding(csv_path)
        return csv_path
    except CSVEncodingError as exc:
        fix = messagebox.askyesno(
            "CSV encoding problem",
            f"{exc}\n\nCreate a repaired copy now? The original file will not be changed.",
        )
        if not fix:
            messagebox.showerror("CSV not loaded", str(exc))
            return ""
        repaired = repair_csv_encoding(csv_path)
        messagebox.showinfo(
            "Repaired copy created",
            f"Undecodable data was replaced with '?' in:\n{repaired}\n\n"
            "Validation will use this copy. Review the '?' values before syncing.",
        )
        return repaired

CONTACT_IMPORT_TEMPLATE_HEADERS = [
    "salutation",
    "firstName",
    "lastName",
    "addressLine1",
    "addressLine2",
    "addressLine3",
    "addressLine4",
    "postalCode",
    "countryIsoCode",
    "email_PRI",
    "email_SEC",
    "email_TER",
    "telephone_PRI",
    "telephone_SEC",
    "telephone_MOB",
    "FAX",
    "messagingVoips",
    "website",
    "isSupplier",
    "isStaff",
    "organisation_name",
    "jobTitle",
    "isReceiveEmailNewsletter",
    "priceList",
    "taxcode",
    "creditLimit",
    "creditTermDays",
    "currencyIso",
    "discountPercentage",
    "taxNumber",
    "staffOwnerName",
    "accountReference",
]

cancel_token = threading.Event()
current_tool_view = "inventory_import"  # default view
API_METHODS = SHOOT_API_METHODS
log_frame = None
shoot_api_frame = None
settings_menu = None
menubar = None
file_menu = None
c_tools_menu = None
gl_tools_menu = None
mtn_tools_menu = None
experimental_menu = None
help_menu = None
export_menu = None
api_method_var = None
api_url_var = None
api_request_text = None
api_response_text = None
api_status_var = None
send_api_button = None
retry_207_button = None
save_log_button = None
save_failed_log_button = None
response_meta_details_var = None
response_meta_details_label = None
response_meta_icon_label = None
current_loadout_name = None
run_response_log = []
failed_response_log = []
MAIN_WINDOW_MIN_SIZE = (700, 576)
performance_graph = None
logo_label_widget = None
ui_mode_compact_var = None
compact_progress_var = None
compact_progress_label = None
compact_placeholder_label = None
compact_mode_enabled = False
menu_label_defaults = {}
full_mode_geometry = None
left_frame = None
right_frame = None
bottom_frame = None
log_viewer = None
scrollbar = None
cancel_btn = None
progress_bar = None
progress_activity_after_id = None
progress_activity_started_at = None
progress_activity_message = "Working"
contact_catalogue_append_var = None
contact_catalogue_first_result_var = None
order_catalogue_append_var = None
order_catalogue_first_result_var = None
inventory_use_pricelist_var = None
inventory_pricelist_var = None
inventory_allow_zero_blanks_var = None
export_product_include_pricelists_var = None
export_product_include_suppliers_var = None
address_contact_lookup_mode_var = None
inventory_pricelist_options = {}
inventory_pricelist_dropdown = None
inventory_pricelist_controls_frame = None
ic_inventory_use_pricelist_var = None
ic_inventory_pricelist_var = None
ic_inventory_generic_value_var = None
ic_inventory_pricelist_options = {}
warehouse_service_var = None
warehouse_service_options = {}
warehouse_service_dropdown = None
warehouse_service_payload_mode_var = None
LOG_LINK_PATTERN = re.compile(
    r"(?:[A-Za-z]:\\|[A-Za-z]:/|/)?[^\s]+\.(?:csv|log|txt)",
    re.IGNORECASE,
)
LOG_LINK_COUNTER = 0

# Ensure the Brightpearl account credentials DB/table exists
def init_db():
    conn = sqlite3.connect(get_credentials_db_path())
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            account_name TEXT PRIMARY KEY,
            app_ref TEXT NOT NULL,
            token TEXT NOT NULL,
            region TEXT NOT NULL CHECK (region IN ('euw1','use1')),
            base_currency TEXT
        )
    """)
    conn.commit()
    conn.close()

# Return a sorted tuple of all saved accounts
def list_saved_accounts():
    init_db()
    conn = sqlite3.connect(get_credentials_db_path())
    cur = conn.cursor()
    cur.execute("SELECT account_name FROM credentials ORDER BY account_name COLLATE NOCASE")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return tuple(rows)

# Shared upsert used by menu dialog and legacy "Registering" panel (if you still show it)
def upsert_credentials(account_name, app_ref, token, region):
    init_db()
    conn = sqlite3.connect(get_credentials_db_path())
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO credentials (account_name, app_ref, token, region)
        VALUES (?,?,?,?)
        ON CONFLICT(account_name) DO UPDATE SET
          app_ref=excluded.app_ref,
          token=excluded.token,
          region=excluded.region
    """, (account_name, app_ref, token, region))
    conn.commit()
    conn.close()

# ---------------- Progress Helpers ----------------
def _format_elapsed_seconds(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(elapsed_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _update_activity_progress_label():
    global progress_activity_after_id
    if progress_activity_started_at is None:
        progress_activity_after_id = None
        return
    elapsed = time.monotonic() - progress_activity_started_at
    update_compact_progress_text(
        f"{progress_activity_message} {_format_elapsed_seconds(elapsed)}"
    )
    progress_activity_after_id = root.after(1000, _update_activity_progress_label)


def progress_start_indeterminate(message="Working"):
    global progress_activity_after_id, progress_activity_started_at, progress_activity_message
    if progress_activity_after_id is not None:
        try:
            root.after_cancel(progress_activity_after_id)
        except Exception:
            pass
        progress_activity_after_id = None
    progress_activity_message = message
    progress_activity_started_at = time.monotonic()
    progress_bar.stop()
    progress_bar.configure(mode="indeterminate")
    progress_bar.start()
    _update_activity_progress_label()

def progress_stop_and_reset_to_determinate():
    global progress_activity_after_id, progress_activity_started_at
    if progress_activity_after_id is not None:
        try:
            root.after_cancel(progress_activity_after_id)
        except Exception:
            pass
        progress_activity_after_id = None
    progress_activity_started_at = None
    progress_bar.stop()
    progress_bar.configure(mode="determinate")
    progress_bar.set(0)
    update_compact_progress(0.0)


def update_compact_progress_text(text: str):
    if compact_progress_var is None:
        return
    compact_progress_var.set(text)


def update_compact_progress(pct: float):
    bounded = max(0.0, min(float(pct), 100.0))
    update_compact_progress_text(f"{bounded:,.0f}%")

PROGRESS_MESSAGE_RE = re.compile(r"PROGRESS\s*:\s*(-?\d+(?:\.\d+)?)")

def apply_progress_from_message(message) -> bool:
    global progress_activity_after_id, progress_activity_started_at
    if not isinstance(message, str):
        return False
    match = PROGRESS_MESSAGE_RE.search(message)
    if not match:
        return False
    try:
        pct = float(match.group(1))
    except (TypeError, ValueError):
        return False
    if progress_activity_after_id is not None:
        try:
            root.after_cancel(progress_activity_after_id)
        except Exception:
            pass
        progress_activity_after_id = None
    progress_activity_started_at = None
    progress_bar.stop()
    progress_bar.configure(mode="determinate")
    bounded = max(0.0, min(pct, 100.0))
    progress_bar.set(bounded / 100.0)
    update_compact_progress(bounded)
    root.update_idletasks()
    return True

# ---------------- Actions (Buttons) ----------------
def run_product_catalogue_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            product_count = update_product_catalogue(
                account_name, db_path,
                log_callback=log_callback,
                cancel_token=cancel_token
            )
            if cancel_token.is_set():
                append_log("🛑 Catalogue sync cancelled.")
                messagebox.showinfo("Cancelled", "Catalogue sync cancelled.")
            else:
                append_log(f"✅ {product_count or 0} products synced.")
                messagebox.showinfo("Done", "Product catalogue synced.")
        except Exception as e:
            messagebox.showerror("Error", f"Catalogue sync failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()
    threading.Thread(target=task, daemon=True).start()


def run_ic_training_action(action, success_message: str, on_complete=None, *, determinate=False):
    """Run an IC Training Helper operation using the standard task UI."""
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return

    def task():
        cancel_token.clear()

        def progress_callback(done, total):
            pct = 100 * done / max(1, total)
            progress_bar.set(pct / 100)
            update_compact_progress(pct)
            root.update_idletasks()

        try:
            if determinate:
                progress_bar.stop()
                progress_bar.configure(mode="determinate")
                progress_bar.set(0)
                update_compact_progress(0)
            else:
                progress_start_indeterminate()
            action_kwargs = {
                "log_callback": log_callback,
                "cancel_token": cancel_token,
            }
            if determinate:
                action_kwargs["progress_callback"] = progress_callback
            action(
                account_name,
                get_data_db_path(account_name),
                **action_kwargs,
            )
            if cancel_token.is_set():
                append_log("🛑 IC Training Helper operation cancelled.")
                messagebox.showinfo("Cancelled", "Operation cancelled.")
            else:
                append_log(f"✅ {success_message}")
                messagebox.showinfo("Done", success_message)
                if on_complete:
                    root.after(0, on_complete)
        except Exception as exc:
            append_log(f"❌ IC Training Helper failed: {exc}")
            messagebox.showerror("Error", f"IC Training Helper failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_export_product_catalogue_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_product_catalogue_export_data(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Export product sync cancelled.")
                messagebox.showinfo("Cancelled", "Export product sync cancelled.")
            else:
                append_log(f"✅ {count or 0} products synced for export.")
                messagebox.showinfo("Done", "Product export sync complete.")
        except Exception as e:
            messagebox.showerror("Error", f"Export product sync failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def run_export_product_catalogue_csv():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return

    file_path = filedialog.asksaveasfilename(
        title="Save Product Catalogue Export As",
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        initialfile=f"{account_name}_product_catalogue_export.csv",
    )
    if not file_path:
        return

    db_path = get_data_db_path(account_name)
    include_price_lists = bool(
        export_product_include_pricelists_var
        and (export_product_include_pricelists_var.get() or "No") == "Yes"
    )
    include_suppliers = bool(
        export_product_include_suppliers_var
        and (export_product_include_suppliers_var.get() or "No") == "Yes"
    )

    def task():
        try:
            progress_start_indeterminate()
            if include_price_lists:
                append_log("➡️ Include price lists enabled. Syncing price lists before export...")
                sync_results = sync_inventory_pricelists(
                    account_name,
                    db_path,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
                if cancel_token.is_set():
                    append_log("🛑 Product export cancelled during pricelist sync.")
                    messagebox.showinfo("Cancelled", "Product export cancelled.")
                    return
                append_log(
                    "✅ Pricelist sync complete before export. "
                    f"Lists: {sync_results.get('price_lists', 0)}, values: {sync_results.get('price_list_values', 0)}."
                )
            if include_suppliers:
                append_log("➡️ Include suppliers enabled. Syncing product suppliers before export...")
                supplier_count = sync_export_product_suppliers(
                    account_name,
                    db_path,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
                if cancel_token.is_set():
                    append_log("🛑 Product export cancelled during supplier sync.")
                    messagebox.showinfo("Cancelled", "Product export cancelled.")
                    return
                append_log(f"✅ Supplier sync complete before export. Rows: {supplier_count}.")
            count = export_synced_product_catalogue_to_csv(
                db_path,
                file_path,
                include_price_lists=include_price_lists,
                include_suppliers=include_suppliers,
            )
            if count <= 0:
                append_log("⚠️ No synced export products found. Sync products before exporting.")
                messagebox.showwarning("No data", "No synced products found. Run Sync products first.")
                return
            append_log(f"📄 Exported {count} products to: {file_path}")
            messagebox.showinfo("Export complete", f"Exported {count} products.")
        except Exception as e:
            messagebox.showerror("Error", f"Product export failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def run_export_product_catalogue_sync_and_export():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return

    file_path = filedialog.asksaveasfilename(
        title="Save Product Catalogue Export As",
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        initialfile=f"{account_name}_product_catalogue_export.csv",
    )
    if not file_path:
        return

    db_path = get_data_db_path(account_name)
    include_price_lists = bool(
        export_product_include_pricelists_var
        and (export_product_include_pricelists_var.get() or "No") == "Yes"
    )
    include_suppliers = bool(
        export_product_include_suppliers_var
        and (export_product_include_suppliers_var.get() or "No") == "Yes"
    )

    def task():
        cancel_token.clear()
        try:
            progress_start_indeterminate()
            synced_count = sync_product_catalogue_export_data(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Sync and export cancelled during sync stage.")
                messagebox.showinfo("Cancelled", "Sync and export cancelled.")
                return
            if include_price_lists:
                append_log("➡️ Include price lists enabled. Syncing price lists before export...")
                sync_results = sync_inventory_pricelists(
                    account_name,
                    db_path,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
                if cancel_token.is_set():
                    append_log("🛑 Sync and export cancelled during pricelist sync stage.")
                    messagebox.showinfo("Cancelled", "Sync and export cancelled.")
                    return
                append_log(
                    "✅ Pricelist sync complete before export. "
                    f"Lists: {sync_results.get('price_lists', 0)}, values: {sync_results.get('price_list_values', 0)}."
                )
            if include_suppliers:
                append_log("➡️ Include suppliers enabled. Syncing product suppliers before export...")
                supplier_count = sync_export_product_suppliers(
                    account_name,
                    db_path,
                    log_callback=log_callback,
                    cancel_token=cancel_token,
                )
                if cancel_token.is_set():
                    append_log("🛑 Sync and export cancelled during supplier sync stage.")
                    messagebox.showinfo("Cancelled", "Sync and export cancelled.")
                    return
                append_log(f"✅ Supplier sync complete before export. Rows: {supplier_count}.")
            exported_count = export_synced_product_catalogue_to_csv(
                db_path,
                file_path,
                include_price_lists=include_price_lists,
                include_suppliers=include_suppliers,
            )
            append_log(
                f"✅ Sync and export complete. Synced {synced_count or 0}, exported {exported_count or 0}."
            )
            messagebox.showinfo(
                "Done",
                f"Sync and export complete. Synced {synced_count or 0}, exported {exported_count or 0}.",
            )
        except Exception as e:
            messagebox.showerror("Error", f"Sync and export failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def run_ip_stock_history_import():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    csv_path = select_csv_for_validation("Select IP Stock History Audit Trail CSV")
    if not csv_path:
        return

    db_path = get_data_db_path(account_name)
    try:
        count = import_ip_stock_history_audit_csv(db_path, csv_path)
        append_log(f"✅ Imported {count} IP stock history audit row(s).")
        messagebox.showinfo("Import complete", f"Imported {count} row(s).")
    except Exception as exc:
        messagebox.showerror("Error", f"IP Stock History import failed:\n{exc}")


def run_ip_stock_history_export():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    output_dir = filedialog.askdirectory(title="Select folder for IP Stock History exports")
    if not output_dir:
        return
    use_xlsx = messagebox.askyesno(
        "Export format",
        "Create Excel (.xlsx) workbooks?\n\nChoose No to create CSV files.",
    )
    file_format = "xlsx" if use_xlsx else "csv"

    db_path = get_data_db_path(account_name)
    try:
        files = export_ip_stock_history_by_warehouse(
            db_path, output_dir, file_format=file_format
        )
        if not files:
            append_log("⚠️ No IP stock history records found to export.")
            messagebox.showwarning("No records", "No IP stock history rows found to export.")
            return
        append_log(f"✅ Exported IP stock history {file_format.upper()} file(s): {len(files)}")
        messagebox.showinfo("Export complete", f"Created {len(files)} file(s).")
    except Exception as exc:
        messagebox.showerror("Error", f"IP Stock History export failed:\n{exc}")


def run_inventory_warehouse_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def warehouse_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            synced = _sync_warehouses_reference_data(
                account_name,
                db_path,
                log_callback=warehouse_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Warehouse sync cancelled.")
                messagebox.showinfo("Cancelled", "Warehouse sync cancelled.")
            else:
                append_log(f"✅ {synced} warehouse(s) synced.")
                messagebox.showinfo("Done", "Warehouses synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Warehouse sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def _sync_warehouses_reference_data(account_name, db_path, *, log_callback, cancel_token):
    credentials = fetch_credentials(account_name)
    results = fetch_and_store_reference_tables(
        account_name,
        credentials.region,
        credentials.headers,
        db_path,
        reference_keys=("warehouses",),
        log_callback=log_callback,
        cancel_token=cancel_token,
    )
    return results.get("warehouses", 0)

def run_inventory_locations_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def location_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            created_count = sync_warehouse_locations(
                account_name,
                db_path,
                log_callback=location_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Location sync cancelled.")
                messagebox.showinfo("Cancelled", "Location sync cancelled.")
            else:
                append_log(f"✅ Location sync complete. Created: {created_count}.")
                messagebox.showinfo("Done", "Locations synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Location sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def refresh_warehouse_service_options():
    global warehouse_service_options, warehouse_service_dropdown
    if warehouse_service_var is None:
        return
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        warehouse_service_options = {}
        warehouse_service_var.set("Select warehouse")
        if warehouse_service_dropdown is not None:
            warehouse_service_dropdown.configure(values=["Select warehouse"])
        return
    db_path = get_data_db_path(account_name)
    warehouse_service_options = get_warehouse_service_options(db_path)
    if warehouse_service_options:
        current = warehouse_service_var.get()
        if current not in warehouse_service_options:
            warehouse_service_var.set(next(iter(warehouse_service_options.keys())))
    else:
        warehouse_service_var.set("No warehouses synced")

    if warehouse_service_dropdown is not None:
        values = list(warehouse_service_options.keys()) or [warehouse_service_var.get()]
        warehouse_service_dropdown.configure(values=values)


def run_warehouse_service_csv_import():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    csv_path = select_csv_for_validation("Select Warehouse Service CSV")
    if not csv_path:
        return

    def task():
        cancel_token.clear()
        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            inserted = import_warehouse_service_csv_tasks(
                account_name,
                db_path,
                csv_path,
                log_callback=log_callback,
            )
            append_log(f"✅ Imported {inserted} warehouse service row(s).")
            messagebox.showinfo("Done", f"Imported {inserted} row(s).")
        except Exception as exc:
            messagebox.showerror("Error", f"CSV import failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def run_warehouse_service_processing():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    selected_label = (warehouse_service_var.get() if warehouse_service_var else "") or ""
    warehouse_id = warehouse_service_options.get(selected_label)
    if warehouse_id is None:
        messagebox.showerror("Error", "Please choose a synced warehouse first.")
        return

    def task():
        cancel_token.clear()
        db_path = get_data_db_path(account_name)
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def warehouse_service_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            single_mode = (warehouse_service_payload_mode_var.get() if warehouse_service_payload_mode_var else "Multi") == "Single"
            processed, total = process_warehouse_service_tasks(
                account_name,
                db_path,
                warehouse_id,
                log_callback=warehouse_service_log_callback,
                cancel_token=cancel_token,
                single_correction_per_call=single_mode,
            )
            if cancel_token.is_set():
                append_log("🛑 Warehouse service processing cancelled.")
                messagebox.showinfo("Cancelled", "Warehouse service processing cancelled.")
            else:
                append_log(f"✅ Warehouse service processing complete ({processed}/{total}).")
                messagebox.showinfo("Done", f"Processed {processed} of {total} row(s).")
        except Exception as exc:
            messagebox.showerror("Error", f"Warehouse service processing failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_contact_catalogue_sync():
    append_choice = (contact_catalogue_append_var.get() if contact_catalogue_append_var else "No") or "No"
    append_enabled = append_choice.strip().lower() in {"yes", "y", "true", "1"}
    first_result_raw = (
        (contact_catalogue_first_result_var.get() if contact_catalogue_first_result_var else "") or ""
    ).strip()

    append_first_result = None
    if append_enabled:
        try:
            append_first_result = int(first_result_raw)
        except ValueError:
            append_first_result = 0
        if append_first_result < 1:
            messagebox.showerror("Error", "Append? = Yes requires firstResult to be a positive integer.")
            return

    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            contact_count = update_contact_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
                append_mode=append_enabled,
                append_first_result=append_first_result,
            )
            if cancel_token.is_set():
                append_log("🛑 Catalogue sync cancelled.")
                messagebox.showinfo("Cancelled", "Catalogue sync cancelled.")
            else:
                append_log(f"✅ {contact_count or 0} contacts synced.")
                messagebox.showinfo("Done", "contact catalogue synced.")
        except Exception as e:
            messagebox.showerror("Error", f"Catalogue sync failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()
    threading.Thread(target=task, daemon=True).start()

def run_sales_order_catalogue_append_sync():
    append_choice = (order_catalogue_append_var.get() if order_catalogue_append_var else "No") or "No"
    append_enabled = append_choice.strip().lower() in {"yes", "y", "true", "1"}
    first_result_raw = (
        (order_catalogue_first_result_var.get() if order_catalogue_first_result_var else "") or ""
    ).strip()

    append_first_result = None
    if append_enabled:
        try:
            append_first_result = int(first_result_raw)
        except ValueError:
            append_first_result = 0
        if append_first_result < 1:
            messagebox.showerror("Error", "Append? = Yes requires lastResult to be a positive integer.")
            return

    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_order_catalogue(
                account_name,
                db_path,
                order_type_id=1,
                log_callback=log_callback,
                cancel_token=cancel_token,
                append_mode=append_enabled,
                append_first_result=append_first_result,
            )
            if cancel_token.is_set():
                append_log("🛑 Sales order catalogue sync cancelled.")
                messagebox.showinfo("Cancelled", "Sales order catalogue sync cancelled.")
            else:
                append_log(f"✅ {count or 0} sales orders synced.")
                messagebox.showinfo("Done", "Sales order catalogue synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Sales order catalogue sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_contact_import_sync_all():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            credentials = fetch_credentials(account_name)
            fetch_and_store_reference_tables(
                account_name,
                credentials.region,
                credentials.headers,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            update_contact_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Contact import sync all cancelled.")
                messagebox.showinfo("Cancelled", "Contact import sync all cancelled.")
            else:
                append_log("✅ Contact reference data and catalogue synced.")
                messagebox.showinfo("Done", "Contact reference data and catalogue synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Contact import sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_product_reference_data_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def ref_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            results = sync_product_reference_data(
                account_name,
                db_path,
                log_callback=ref_log_callback,
                cancel_token=cancel_token,
            )
            pcf_count = sync_product_custom_fields(
                account_name,
                db_path,
                log_callback=ref_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Product reference sync cancelled.")
                messagebox.showinfo("Cancelled", "Product reference sync cancelled.")
            else:
                summary = ", ".join(f"{k}: {v}" for k, v in results.items())
                append_log(f"✅ Product reference data synced. {summary}, productPCFs: {pcf_count or 0}")
                messagebox.showinfo("Done", "Product reference data synced.")
        except Exception as e:
            messagebox.showerror("Error", f"Product reference sync failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def download_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save CSV Template As",
        initialfile="bp_inventory_import.csv",
    )
    if not file_path:
        return
    headers = ["sku", "quantity", "locationName", "costprice", "warehouseId"]
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
    append_log(f"📄 Template saved to: {file_path}")
    
def download_address_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save CSV Template As",
        initialfile="bp_addresses_import.csv",
    )
    if not file_path:
        return
    headers = ["emailAddress", "contactId", "addressLine1", "addressLine2", "addressLine3", "addressLine4", "postalCode", "countryIsoCode"]
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
    append_log(f"📄 Template saved to: {file_path}")

def download_contact_ids_csv():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return

    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Contact IDs CSV As",
        initialfile="contact_ids.csv",
    )
    if not file_path:
        return

    db_path = get_data_db_path(account_name)
    if not os.path.exists(db_path):
        messagebox.showerror("Error", f"Database not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT contactId, firstName, lastName, companyName, isSupplier
            FROM contact_catalogue
            ORDER BY contactId
            """
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        messagebox.showerror("Error", f"Failed to read contact catalogue: {exc}")
        return
    finally:
        conn.close()

    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["contactId", "firstName", "lastName", "company", "isSupplier"])
        writer.writerows(rows)

    append_log(f"📄 Exported {len(rows)} contact ids to: {file_path}")

def download_contact_import_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Contact Import CSV Template As",
        initialfile="contact_import.csv",
    )
    if not file_path:
        return
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CONTACT_IMPORT_TEMPLATE_HEADERS)
    append_log(f"📄 Contact import template saved to: {file_path}")

def download_product_import_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Product Import CSV Template As",
        initialfile="bp_product_import.csv",
    )
    if not file_path:
        return
    headers = list(PRODUCT_IMPORT_TEMPLATE_HEADERS)
    account_name = (selected_account_var.get() or "").strip()
    if account_name:
        db_path = get_data_db_path(account_name)
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT code
                    FROM ref_pcf_product
                    WHERE code LIKE 'PCF_%'
                    ORDER BY code
                    """
                )
                pcf_headers = [row[0] for row in cur.fetchall() if row and row[0]]
                headers.extend(pcf_headers)
            except sqlite3.Error:
                pass
            finally:
                conn.close()

    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
    append_log(f"📄 Product import template saved to: {file_path}")

def download_product_id_sku_reference():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save ProductId/SKU Reference As",
        initialfile="productIdSkuRefData.csv",
    )
    if not file_path:
        return
    db_path = get_data_db_path(account_name)
    count = export_product_id_sku_reference(db_path, file_path)
    append_log(f"📄 Exported {count} product ids to: {file_path}")

def open_product_update_picker():
    picker = ctk.CTkToplevel(root)
    picker.title("Product Update Picker")
    picker.geometry("520x520")
    picker.grab_set()

    identifier_var = StringVar(value="productId")

    ctk.CTkLabel(picker, text="Select fields to update:", font=("Segoe UI", 12, "bold")).pack(
        anchor="w", padx=12, pady=(12, 4)
    )

    scroll_frame = ctk.CTkScrollableFrame(picker, height=320)
    scroll_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    grouped_fields = {
        "identity": [field for field in PRODUCT_UPDATE_FIELDS if field.startswith("identity.")],
        "stock": [
            field
            for field in PRODUCT_UPDATE_FIELDS
            if field.startswith("stock.") or field.startswith("dimensions.")
        ],
        "financialDetails": [
            field for field in PRODUCT_UPDATE_FIELDS if field.startswith("financialDetails.")
        ],
        "salesChannels": [
            field for field in PRODUCT_UPDATE_FIELDS if field.startswith("salesChannels.")
        ],
        "variations": [field for field in PRODUCT_UPDATE_FIELDS if field == "variations"],
        "composition": [
            field for field in PRODUCT_UPDATE_FIELDS if field.startswith("composition.")
        ],
    }
    field_group_map = {
        field: group for group, fields in grouped_fields.items() for field in fields
    }

    field_vars = {}

    def apply_group_selection(field: str) -> None:
        group = field_group_map.get(field)
        if not group:
            return
        group_fields = grouped_fields.get(group, [])
        value = field_vars[field].get()
        for group_field in group_fields:
            field_vars[group_field].set(value)

    for field in PRODUCT_UPDATE_FIELDS:
        var = tk.BooleanVar(value=False)
        checkbox = ctk.CTkCheckBox(
            scroll_frame,
            text=field,
            variable=var,
            command=lambda field=field: apply_group_selection(field),
        )
        checkbox.pack(anchor="w", pady=2)
        field_vars[field] = var

    radio_frame = ctk.CTkFrame(picker, fg_color="transparent")
    radio_frame.pack(fill="x", padx=12, pady=(0, 12))
    ctk.CTkLabel(radio_frame, text="Identifier column:").pack(side="left")
    ctk.CTkRadioButton(
        radio_frame, text="productId", variable=identifier_var, value="productId"
    ).pack(side="left", padx=6)
    ctk.CTkRadioButton(
        radio_frame, text="sku", variable=identifier_var, value="sku"
    ).pack(side="left", padx=6)

    def handle_create():
        selected_fields = [field for field, var in field_vars.items() if var.get()]
        if not selected_fields:
            messagebox.showwarning("No fields", "Select at least one field to update.")
            return
        identifier = identifier_var.get()
        if identifier == "sku" and "identity.sku" in selected_fields:
            messagebox.showerror(
                "Invalid selection",
                "Updating SKU requires productId as the identifier.",
            )
            return
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
            title="Save Product Update Template As",
            initialfile="product_update.csv",
        )
        if not file_path:
            return
        create_product_update_template(file_path, identifier, selected_fields)
        append_log(f"📄 Product update template saved to: {file_path}")
        picker.destroy()

    ctk.CTkButton(picker, text="Create", command=handle_create).pack(
        pady=(0, 12)
    )

def download_location_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save CSV Template As",
        initialfile="bp_locations_import.csv",
    )
    if not file_path:
        return
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(WAREHOUSE_LOCATION_TEMPLATE_HEADERS)
    append_log(f"📄 Template saved to: {file_path}")

def download_all_locations_csv():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Locations Export As",
        initialfile="warehouse_locations_export.csv",
    )
    if not file_path:
        return
    db_path = get_data_db_path(account_name)
    count = export_locations_csv(db_path, file_path, log_callback=log_callback)
    append_log(f"📄 Exported {count} locations to: {file_path}")

def download_zone_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Warehouse Zones CSV Template As",
        initialfile="bp_zones_import.csv",
    )
    if not file_path:
        return
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(WAREHOUSE_ZONE_TEMPLATE_HEADERS)
    append_log(f"📄 Template saved to: {file_path}")

def download_historic_orders_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Historic Orders CSV Template As",
        initialfile="bp_historic_import.csv",
    )
    if not file_path:
        return
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(HISTORIC_ORDER_TEMPLATE_HEADERS)
    append_log(f"📄 Historic orders template saved to: {file_path}")

def download_sales_orders_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Sales Orders CSV Template As",
        initialfile="bp_sales_import.csv",
    )
    if not file_path:
        return
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(SALES_ORDER_TEMPLATE_HEADERS)
    append_log(f"📄 Sales orders template saved to: {file_path}")

def download_open_purchases_csv_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Open Purchases CSV Template As",
        initialfile="bp_purchases_import.csv",
    )
    if not file_path:
        return
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(OPEN_PURCHASE_TEMPLATE_HEADERS)
    append_log(f"📄 Open purchases template saved to: {file_path}")

def download_contact_pcf_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Contact Custom Fields Template As",
        initialfile="bp_pcfcontact_import.csv",
    )
    if not file_path:
        return
    headers = ["contactEmail", "replacePcf_codesHere"]
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
    append_log(f"📄 Template saved to: {file_path}")

def download_product_pcf_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Product Custom Fields Template As",
        initialfile="bp_pcfproduct_import.csv",
    )
    if not file_path:
        return
    headers = ["sku", "replacePcf_codesHere"]
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
    append_log(f"📄 Template saved to: {file_path}")

def download_order_pcf_template():
    file_path = filedialog.asksaveasfilename(
        defaultextension=".csv",
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Save Order Custom Fields Template As",
        initialfile="bp_pcforder_import.csv",
    )
    if not file_path:
        return
    headers = ["orderRef", "replacePcf_codeHere"]
    with open_table(file_path, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
    append_log(f"📄 Template saved to: {file_path}")

def run_inventory_csv_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return
        try:
            progress_start_indeterminate()
            inserted_count = validate_and_enrich_inventory(
                csv_path, db_path, account_name,
                region=None, log_callback=log_callback, cancel_token=cancel_token
            )
            if cancel_token.is_set():
                append_log(f"🛑 Validation cancelled. Inserted so far: {inserted_count or 0}.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(f"✅ Validation complete. Inserted: {inserted_count or 0}.")
                messagebox.showinfo("Done", "CSV validated and enriched.")
        except Exception as e:
            messagebox.showerror("Error", f"CSV validation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()
    threading.Thread(target=task, daemon=True).start()

def run_address_csv_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return
        try:
            progress_start_indeterminate()
            inserted_count = validate_addresses(
                csv_path,
                db_path,
                account_name,
                region=None,
                log_callback=log_callback,
                cancel_token=cancel_token,
                contact_lookup_mode=(address_contact_lookup_mode_var.get() if address_contact_lookup_mode_var else "email"),
            )
            if cancel_token.is_set():
                append_log(f"🛑 Validation cancelled. Inserted so far: {inserted_count or 0}.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(f"✅ Validation complete. Inserted: {inserted_count or 0}.")
                messagebox.showinfo("Done", "CSV validated and enriched.")
        except Exception as e:
            messagebox.showerror("Error", f"CSV validation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()
    threading.Thread(target=task, daemon=True).start()

def run_contact_import_csv_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return
        try:
            progress_start_indeterminate()
            inserted_count = validate_contacts(
                csv_path,
                db_path,
                account_name,
                region=None,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log(f"🛑 Validation cancelled. Inserted so far: {inserted_count or 0}.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(f"✅ Validation complete. Inserted: {inserted_count or 0}.")
                messagebox.showinfo("Done", "CSV validated and enriched.")
        except Exception as e:
            messagebox.showerror("Error", f"CSV validation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_product_import_csv_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return
        try:
            progress_start_indeterminate()
            summary = validate_product_import_csv(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            validated_custom_fields = validate_product_custom_fields_from_import_csv(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Product import validation cancelled.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(
                    "✅ Product import validation complete. "
                    f"Inserted: {summary.get('inserted', 0)}. "
                    f"Validated custom fields: {validated_custom_fields}."
                )
                missing_seasons = summary.get("missing_seasons", [])
                if missing_seasons:
                    messagebox.showwarning(
                        "Missing seasons",
                        "Some seasons do not exist and will need to be manually created. "
                        "See unmatched report for details.",
                    )
                duplicate_skus = summary.get("duplicate_skus", [])
                if duplicate_skus:
                    messagebox.showwarning(
                        "Duplicate SKUs",
                        "Duplicate SKUs were detected in the CSV. "
                        "See the exceptions report for details.",
                    )
                messagebox.showinfo("Done", "CSV validated and staged for import.")
        except Exception as e:
            messagebox.showerror("Error", f"CSV validation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()
    threading.Thread(target=task, daemon=True).start()

def run_product_update_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def update_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            summary = validate_product_update_csv(
                csv_path,
                db_path,
                account_name,
                log_callback=update_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Product update validation cancelled.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(
                    "✅ Product update validation complete. "
                    f"Inserted: {summary.get('inserted', 0)}."
                )
                if summary.get("invalid_rows", 0):
                    messagebox.showwarning(
                        "Validation issues",
                        "Some rows had validation issues. See the unmatched report for details.",
                    )
                if summary.get("duplicate_variations", 0):
                    messagebox.showwarning(
                        "Duplicate variations detected",
                        "Some variations already existed on products. They will remain in the payload.",
                    )
                messagebox.showinfo("Done", "Product update CSV validated.")
        except Exception as e:
            messagebox.showerror("Error", f"CSV validation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_create_missing_product_update_references():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            results = create_missing_product_update_references(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Update reference creation cancelled.")
                messagebox.showinfo("Cancelled", "Update reference creation cancelled.")
            else:
                summary = ", ".join(f"{k}: {v}" for k, v in results.items())
                append_log(f"✅ Update references created. {summary}")
                messagebox.showinfo(
                    "Update references created",
                    "Re-validate your update CSV to refresh option lookups.",
                )
        except Exception as e:
            messagebox.showerror("Error", f"Update reference creation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_product_update_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def update_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            updated_count = sync_product_updates(
                account_name,
                db_path,
                log_callback=update_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Product update sync cancelled.")
                messagebox.showinfo("Cancelled", "Product update sync cancelled.")
            else:
                append_log(f"✅ Product updates complete. Updated: {updated_count}.")
                messagebox.showinfo("Done", "Product updates synced to Brightpearl.")
        except Exception as e:
            messagebox.showerror("Error", f"Product update sync failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_create_missing_product_references():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            results = create_missing_product_references(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Reference creation cancelled.")
                messagebox.showinfo("Cancelled", "Reference creation cancelled.")
            else:
                summary = ", ".join(f"{k}: {v}" for k, v in results.items())
                append_log(f"✅ Missing references created. {summary}")
                messagebox.showinfo(
                    "Reference data created",
                    "We recommend re-uploading your data file to trigger a validation check.",
                )
        except Exception as e:
            messagebox.showerror("Error", f"Reference creation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()
    threading.Thread(target=task, daemon=True).start()

def run_create_products_from_import():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def product_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            created_count = create_products_from_import(
                account_name,
                db_path,
                log_callback=product_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Product creation cancelled.")
                messagebox.showinfo("Cancelled", "Product creation cancelled.")
            else:
                append_log(f"✅ Product creation complete. Created: {created_count}.")
                pcf_synced = sync_custom_fields_to_brightpearl(
                    account_name,
                    db_path,
                    log_callback=product_log_callback,
                    cancel_token=cancel_token,
                )
                append_log(f"✅ Product custom fields synced after product import: {pcf_synced}.")
                messagebox.showinfo("Done", "Products and custom fields synced to Brightpearl.")
        except Exception as e:
            messagebox.showerror("Error", f"Product creation failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def run_location_catalogue_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            location_count = update_location_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Catalogue sync cancelled.")
                messagebox.showinfo("Cancelled", "Catalogue sync cancelled.")
            else:
                append_log(f"✅ {location_count or 0} locations synced.")
                messagebox.showinfo("Done", "Location catalogue synced.")
        except Exception as e:
            messagebox.showerror("Error", f"Catalogue sync failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_zone_catalogue_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            zone_count = update_zone_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Catalogue sync cancelled.")
                messagebox.showinfo("Cancelled", "Catalogue sync cancelled.")
            else:
                append_log(f"✅ {zone_count or 0} zones synced.")
                messagebox.showinfo("Done", "Zone catalogue synced.")
        except Exception as e:
            messagebox.showerror("Error", f"Catalogue sync failed:\n{e}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_warehouse_locations_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return

        try:
            progress_start_indeterminate()
            inserted_count = validate_warehouse_locations(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log(f"🛑 Location validation cancelled. Inserted so far: {inserted_count}.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(f"✅ Location validation complete. Inserted: {inserted_count}.")
                messagebox.showinfo("Done", "CSV validated.")
        except Exception as exc:
            messagebox.showerror("Error", f"CSV validation failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_warehouse_location_updates_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return

        try:
            progress_start_indeterminate()
            summary = validate_location_updates(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Location update validation cancelled.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(
                    "✅ Location update validation complete. "
                    f"Inserted: {summary.get('inserted', 0)}."
                )
                if summary.get("unmatched", 0) or summary.get("invalid", 0):
                    messagebox.showwarning(
                        "Validation issues",
                        "Some rows had validation issues. See the unmatched report for details.",
                    )
                messagebox.showinfo("Done", "Location update CSV validated.")
        except Exception as exc:
            messagebox.showerror("Error", f"CSV validation failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_warehouse_zones_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return

        try:
            progress_start_indeterminate()
            inserted_count = validate_warehouse_zones(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log(f"🛑 Zone validation cancelled. Inserted so far: {inserted_count}.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(f"✅ Zone validation complete. Inserted: {inserted_count}.")
                messagebox.showinfo("Done", "CSV validated.")
        except Exception as exc:
            messagebox.showerror("Error", f"CSV validation failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_historic_orders_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return

        try:
            progress_start_indeterminate()
            orders_inserted, rows_inserted = validate_historic_orders(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
            )
            if cancel_token.is_set():
                append_log("🛑 Historic orders validation cancelled.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(
                    f"✅ Historic orders validated. Orders: {orders_inserted}, rows: {rows_inserted}."
                )
                messagebox.showinfo("Done", "Historic orders CSV validated.")
        except Exception as exc:
            messagebox.showerror("Error", f"Historic orders validation failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_sales_orders_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return

        try:
            progress_start_indeterminate()
            orders_inserted, rows_inserted = validate_sales_orders(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
            )
            if cancel_token.is_set():
                append_log("🛑 Sales orders validation cancelled.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(
                    f"✅ Sales orders validated. Orders: {orders_inserted}, rows: {rows_inserted}."
                )
                messagebox.showinfo("Done", "Sales orders CSV validated.")
        except Exception as exc:
            messagebox.showerror("Error", f"Sales orders validation failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_open_purchases_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return

        try:
            progress_start_indeterminate()
            orders_inserted, rows_inserted = validate_open_purchases(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
            )
            if cancel_token.is_set():
                append_log("🛑 Open purchases validation cancelled.")
                messagebox.showinfo("Cancelled", "Validation cancelled.")
            else:
                append_log(
                    f"✅ Open purchases validated. Orders: {orders_inserted}, rows: {rows_inserted}."
                )
                messagebox.showinfo("Done", "Open purchases CSV validated.")
        except Exception as exc:
            messagebox.showerror("Error", f"Open purchases validation failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_customer_pcf_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_contact_custom_fields(
                account_name,
                db_path,
                contact_type="customer",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Customer PCF sync cancelled.")
                messagebox.showinfo("Cancelled", "Customer custom fields sync cancelled.")
            else:
                append_log(f"✅ {count or 0} customer custom fields synced.")
                messagebox.showinfo("Done", "Customer custom fields synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Customer custom field sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_supplier_pcf_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_contact_custom_fields(
                account_name,
                db_path,
                contact_type="supplier",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Supplier PCF sync cancelled.")
                messagebox.showinfo("Cancelled", "Supplier custom fields sync cancelled.")
            else:
                append_log(f"✅ {count or 0} supplier custom fields synced.")
                messagebox.showinfo("Done", "Supplier custom fields synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Supplier custom field sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_product_pcf_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_product_custom_fields(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Product PCF sync cancelled.")
                messagebox.showinfo("Cancelled", "Product custom fields sync cancelled.")
            else:
                append_log(f"✅ {count or 0} product custom fields synced.")
                messagebox.showinfo("Done", "Product custom fields synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Product custom field sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_sales_pcf_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_order_custom_fields(
                account_name,
                db_path,
                order_type="sales",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Sales PCF sync cancelled.")
                messagebox.showinfo("Cancelled", "Sales custom fields sync cancelled.")
            else:
                append_log(f"✅ {count or 0} sales custom fields synced.")
                messagebox.showinfo("Done", "Sales custom fields synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Sales custom field sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_purchase_pcf_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_order_custom_fields(
                account_name,
                db_path,
                order_type="purchase",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Purchase PCF sync cancelled.")
                messagebox.showinfo("Cancelled", "Purchase custom fields sync cancelled.")
            else:
                append_log(f"✅ {count or 0} purchase custom fields synced.")
                messagebox.showinfo("Done", "Purchase custom fields synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Purchase custom field sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_sales_order_catalogue_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_order_catalogue(
                account_name,
                db_path,
                order_type_id=1,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Sales order catalogue sync cancelled.")
                messagebox.showinfo("Cancelled", "Sales order catalogue sync cancelled.")
            else:
                append_log(f"✅ {count or 0} sales orders synced.")
                messagebox.showinfo("Done", "Sales order catalogue synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Sales order catalogue sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_purchase_order_catalogue_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            count = sync_order_catalogue(
                account_name,
                db_path,
                order_type_id=2,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Purchase order catalogue sync cancelled.")
                messagebox.showinfo("Cancelled", "Purchase order catalogue sync cancelled.")
            else:
                append_log(f"✅ {count or 0} purchase orders synced.")
                messagebox.showinfo("Done", "Purchase order catalogue synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Purchase order catalogue sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_custom_fields_sync_all():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            total = 0
            total += sync_contact_custom_fields(
                account_name,
                db_path,
                contact_type="customer",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            total += sync_contact_custom_fields(
                account_name,
                db_path,
                contact_type="supplier",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            total += sync_product_custom_fields(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            total += sync_order_custom_fields(
                account_name,
                db_path,
                order_type="sales",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            total += sync_order_custom_fields(
                account_name,
                db_path,
                order_type="purchase",
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Custom field sync all cancelled.")
                messagebox.showinfo("Cancelled", "Custom field sync all cancelled.")
            else:
                append_log(f"✅ {total or 0} custom fields synced.")
                messagebox.showinfo("Done", "Custom field reference data synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Custom field sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_pcf_csv_validation():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        csv_path = select_csv_for_validation()
        if not csv_path:
            messagebox.showwarning("Cancelled", "No file selected.")
            return

        try:
            progress_start_indeterminate()
            inserted = validate_custom_fields_csv(
                csv_path,
                db_path,
                account_name,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Custom fields validation cancelled.")
                messagebox.showinfo("Cancelled", "Custom fields validation cancelled.")
            else:
                append_log(f"✅ Custom fields validated. Inserted: {inserted}.")
                messagebox.showinfo("Done", "Custom fields CSV validated.")
        except Exception as exc:
            messagebox.showerror("Error", f"Custom fields validation failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_pcf_sync_to_brightpearl():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            synced = sync_custom_fields_to_brightpearl(
                account_name,
                db_path,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Custom fields sync cancelled.")
                messagebox.showinfo("Cancelled", "Custom fields sync cancelled.")
            else:
                append_log(f"✅ Custom fields synced: {synced}.")
                messagebox.showinfo("Done", "Custom fields synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Custom fields sync failed:\n{exc}")
        finally:
            progress_bar.set(0)

    threading.Thread(target=task, daemon=True).start()

def run_inventory_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            allow_choice = (inventory_allow_zero_blanks_var.get() if inventory_allow_zero_blanks_var else "Yes") or "Yes"
            allow_zero_blanks = allow_choice.strip().lower() in {"yes", "y", "true", "1"}
            sync_inventory(
                account_name,
                db_path,
                progress_callback=progress_callback,
                log_callback=log_callback,
                cancel_token=cancel_token,
                allow_zero_blanks=allow_zero_blanks,
            )
            if cancel_token.is_set():
                append_log("🛑 Inventory sync cancelled.")
                messagebox.showinfo("Cancelled", "Inventory sync cancelled.")
            else:
                append_log("✅ Inventory sync complete.")
                messagebox.showinfo("Done", "Inventory synced to Brightpearl.")
        except Exception as e:
            messagebox.showerror("Error", f"Inventory sync failed:\n{e}")
        finally:
            progress_bar.set(0)
    threading.Thread(target=task, daemon=True).start()

def run_historic_orders_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            sync_historic_orders(
                account_name,
                db_path,
                log_callback=log_callback,
                progress_callback=progress_callback,
            )
            if cancel_token.is_set():
                append_log("🛑 Historic orders sync cancelled.")
                messagebox.showinfo("Cancelled", "Historic orders sync cancelled.")
            else:
                append_log("✅ Historic orders synced to Brightpearl.")
                messagebox.showinfo("Done", "Historic orders synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Historic orders sync failed:\n{exc}")
        finally:
            progress_bar.set(0)

    threading.Thread(target=task, daemon=True).start()

def run_sales_orders_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            sync_sales_orders(
                account_name,
                db_path,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Sales orders sync cancelled.")
                messagebox.showinfo("Cancelled", "Sales orders sync cancelled.")
            else:
                append_log("✅ Sales orders synced to Brightpearl.")
                messagebox.showinfo("Done", "Sales orders synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Sales orders sync failed:\n{exc}")
        finally:
            progress_bar.set(0)

    threading.Thread(target=task, daemon=True).start()

def run_open_purchases_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            sync_open_purchases(
                account_name,
                db_path,
                log_callback=log_callback,
                progress_callback=progress_callback,
            )
            if cancel_token.is_set():
                append_log("🛑 Open purchases sync cancelled.")
                messagebox.showinfo("Cancelled", "Open purchases sync cancelled.")
            else:
                append_log("✅ Open purchases synced to Brightpearl.")
                messagebox.showinfo("Done", "Open purchases synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Open purchases sync failed:\n{exc}")
        finally:
            progress_bar.set(0)

    threading.Thread(target=task, daemon=True).start()

def run_reference_data_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def ref_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            credentials = fetch_credentials(account_name)
            fetch_and_store_reference_tables(
                account_name,
                credentials.region,
                credentials.headers,
                db_path,
                log_callback=ref_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Reference data sync cancelled.")
                messagebox.showinfo("Cancelled", "Reference data sync cancelled.")
            else:
                append_log("✅ Reference data sync complete.")
                messagebox.showinfo("Done", "Reference data synced.")
        except Exception as exc:
            messagebox.showerror("Error", f"Reference data sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_address_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            sync_addresses(
                account_name, db_path,
                progress_callback=progress_callback, log_callback=log_callback, cancel_token=cancel_token
            )
            if cancel_token.is_set():
                append_log("🛑 address sync cancelled.")
                messagebox.showinfo("Cancelled", "address sync cancelled.")
            else:
                append_log("✅ Address sync complete.")
                messagebox.showinfo("Done", "Addresses synced to Brightpearl.")
        except Exception as e:
            messagebox.showerror("Error", f"address sync failed:\n{e}")
        finally:
            progress_bar.set(0)
    threading.Thread(target=task, daemon=True).start()

def run_contact_import_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            sync_contacts(
                account_name,
                db_path,
                progress_callback=progress_callback,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Contact import sync cancelled.")
                messagebox.showinfo("Cancelled", "Contact import sync cancelled.")
            else:
                append_log("✅ Contacts created in Brightpearl.")
                messagebox.showinfo("Done", "Contacts created in Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Contact import sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_warehouse_locations_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def location_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            created_count = sync_warehouse_locations(
                account_name,
                db_path,
                log_callback=location_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Location sync cancelled.")
                messagebox.showinfo("Cancelled", "Location sync cancelled.")
            else:
                append_log(f"✅ Location sync complete. Created: {created_count}.")
                messagebox.showinfo("Done", "Locations synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Location sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_warehouse_locations_update_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            def update_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            updated_count = sync_location_updates(
                account_name,
                db_path,
                log_callback=update_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Location updates cancelled.")
                messagebox.showinfo("Cancelled", "Location updates cancelled.")
            else:
                append_log(f"✅ Location updates complete. Updated: {updated_count}.")
                messagebox.showinfo("Done", "Location updates synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Location update sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_warehouse_zones_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            def zone_log_callback(message):
                if apply_progress_from_message(message):
                    return
                append_log(message)

            created_count = sync_warehouse_zones(
                account_name,
                db_path,
                log_callback=zone_log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Zone sync cancelled.")
                messagebox.showinfo("Cancelled", "Zone sync cancelled.")
            else:
                append_log(f"✅ Zone sync complete. Created: {created_count}.")
                messagebox.showinfo("Done", "Zones synced to Brightpearl.")
        except Exception as exc:
            messagebox.showerror("Error", f"Zone sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_sync_forget_contacts_catalogue():
    def task():
        cancel_token.clear()

        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        # Read the GUI dates and push them into env vars
        os.environ["BP_CONTACTS_FROM_DATE"] = from_date_var.get().strip()
        os.environ["BP_CONTACTS_TO_DATE"]   = to_date_var.get().strip()

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            db_path = get_data_db_path(account_name)

            update_forget_contacts_catalogue(
                account_name=account_name,
                db_path=db_path,
                log_callback=log_callback,
                cancel_token=cancel_token
            )

            if cancel_token.is_set():
                append_log("🛑 Sync Contacts to Forget cancelled.")
                messagebox.showinfo("Cancelled", "Sync cancelled.")
            else:
                append_log("✅ Sync Contacts to Forget complete.")
                messagebox.showinfo("Done", "Contacts stored in forget_contacts.")
        except Exception as e:
            messagebox.showerror("Error", f"Sync Contacts to Forget failed:\n{e}")
        finally:
            progress_bar.set(0)

    threading.Thread(target=task, daemon=True).start()

def run_forget_contacts():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            forget_contacts_worker(
                account_name=account_name,
                progress_callback=progress_callback,
                log_callback=log_callback,
                cancel_token=cancel_token
            )

            if cancel_token.is_set():
                append_log("🛑 Forget contacts cancelled.")
                messagebox.showinfo("Cancelled", "Forget contacts cancelled.")
            else:
                append_log("✅ Forget contacts routine complete.")
                messagebox.showinfo("Done", "Attempted GDPR forget on all pending contacts.")
        except Exception as e:
            messagebox.showerror("Error", f"Forget contacts failed:\n{e}")
        finally:
            progress_bar.set(0)

    threading.Thread(target=task, daemon=True).start()
    

def run_forget_contact_orders():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        def progress_callback(done, total):
            pct = int(100 * done / max(1, total))
            progress_bar.configure(mode="determinate")
            progress_bar.set(pct / 100)
            root.update_idletasks()

        try:
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)

            forget_contact_orders_worker(
                account_name=account_name,
                progress_callback=progress_callback,
                log_callback=log_callback,
                cancel_token=cancel_token
            )

            if cancel_token.is_set():
                append_log("🛑 Remove from orders cancelled.")
                messagebox.showinfo("Cancelled", "Remove from orders cancelled.")
            else:
                append_log("✅ Remove from orders routine complete.")
                messagebox.showinfo("Done", "Attempted order forget on all pending contacts.")
        except Exception as e:
            messagebox.showerror("Error", f"Remove from orders failed:\n{e}")
        finally:
            progress_bar.set(0)

    threading.Thread(target=task, daemon=True).start()

def cancel_task():
    cancel_token.set()
    append_log("🔴 Cancel requested by user.")
    progress_bar.stop()

def refresh_shoot_api_defaults():
    if current_tool_view != "shoot_apis":
        return
    if api_url_var is not None:
        api_url_var.set((api_url_var.get() or "").strip())

def send_api_request():
    if send_api_button is None:
        return

    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            root.after(0, lambda: messagebox.showerror("Error", "Choose an Active account first (top-right)."))
            return

        try:
            creds = fetch_credentials(account_name)
            db_path = get_shoot_api_db_path(account_name)
        except Exception as exc:
            root.after(0, lambda: messagebox.showerror("Error", f"Unable to load credentials:\n{exc}"))
            return

        method = (api_method_var.get() or "").strip().upper()
        if method not in API_METHODS:
            root.after(0, lambda: messagebox.showerror("Error", "Select a valid HTTP method."))
            return

        variables = load_variables(db_path)
        raw_url = api_url_var.get().strip()
        resolved_url = replace_template_vars(raw_url, variables)
        full_url = normalize_api_url(resolved_url, account_name, creds.region)
        if not full_url:
            root.after(0, lambda: messagebox.showerror("Error", "Enter an API URL."))
            return

        payload_text = api_request_text.get("1.0", END).strip() if api_request_text is not None else ""
        payload_text = replace_template_vars(payload_text, variables)
        json_payload, payload_error = parse_json_payload(payload_text)
        if payload_error is not None:
            root.after(0, lambda: messagebox.showerror("Invalid JSON", f"JSON body is invalid:\n{payload_error}"))
            return

        headers = {
            "brightpearl-app-ref": creds.app_ref,
            "brightpearl-account-token": creds.token,
        }

        root.after(0, lambda: progress_start_indeterminate())
        root.after(0, lambda: send_api_button.configure(state="disabled"))
        if retry_207_button is not None:
            root.after(0, lambda: retry_207_button.configure(state="disabled"))
        root.after(0, lambda: api_status_var.set("Sending request..."))

        response_text = ""
        status_text = ""
        next_throttle = 0
        updated_vars = []
        try:
            api_response = execute_api_request(
                method,
                full_url,
                headers,
                json_payload,
                timeout=30,
            )
            response_text = api_response.response_text
            next_throttle = api_response.next_throttle
            remaining = api_response.remaining
            if api_response.status_code is None:
                status_text = "Request failed."
            else:
                status_text = f"Status: {api_response.status_code}"
            if remaining is not None:
                status_text += f" | Remaining: {remaining}"
            if next_throttle > 0:
                status_text += f" | Throttle: {next_throttle}ms"
            updated_vars = apply_response_variable_mappings(
                db_path,
                current_loadout_name,
                response_text,
            )
        except Exception as exc:
            response_text = f"Request failed:\n{exc}"
            status_text = "Request failed."
        finally:
            def finalize():
                if api_response_text is not None:
                    api_response_text.configure(state="normal")
                    api_response_text.delete("1.0", END)
                    api_response_text.insert("1.0", response_text)
                    api_response_text.configure(state="disabled")
                update_response_meta(response_text)
                api_status_var.set(status_text)
                progress_stop_and_reset_to_determinate()
                send_api_button.configure(state="normal")
                if retry_207_button is not None:
                    retry_207_button.configure(state="normal")
                if updated_vars:
                    append_log(
                        "🔖 Updated variables from response: "
                        f"{', '.join(updated_vars)}."
                    )
                    root.event_generate("<<VariablesUpdated>>")

            root.after(0, finalize)

            if next_throttle > 0:
                sleep_with_cancel_ms(next_throttle, cancel_token=cancel_token)

    threading.Thread(target=task, daemon=True).start()

def save_current_api_loadout():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    if api_url_var is None or api_request_text is None:
        messagebox.showerror("Error", "API panel is not ready yet.")
        return

    url = (api_url_var.get() or "").strip()
    if not url:
        messagebox.showerror("Error", "Enter an API URL before saving a loadout.")
        return

    payload_text = api_request_text.get("1.0", END).strip()
    if payload_text:
        try:
            json.loads(payload_text)
        except json.JSONDecodeError as exc:
            messagebox.showerror("Invalid JSON", f"JSON body is invalid:\n{exc}")
            return
    method = (api_method_var.get() or "").strip().upper()
    if method not in API_METHODS:
        messagebox.showerror("Error", "Select a valid HTTP method before saving.")
        return

    db_path = get_shoot_api_db_path(account_name)
    ensure_loadouts_table(db_path)

    window = ctk.CTkToplevel(root)
    window.title("Save Loadout")
    window.geometry("360x180")
    window.grab_set()
    bring_window_to_front(window)
    settings = get_settings()
    label_font, control_font = get_dialog_fonts(settings)
    label_pad, field_pad = get_dialog_spacing(settings)

    name_var = StringVar()
    ctk.CTkLabel(window, text="Quickname", font=label_font).pack(
        anchor="w",
        padx=12,
        pady=(field_pad, label_pad),
    )
    name_entry = ctk.CTkEntry(window, textvariable=name_var, font=control_font)
    name_entry.pack(fill="x", padx=12, pady=(0, field_pad))

    def handle_save():
        quickname = name_var.get().strip()
        if not quickname:
            messagebox.showerror("Error", "Quickname is required.")
            return
        existing = fetch_api_loadout(db_path, quickname)
        if existing and not messagebox.askyesno(
            "Overwrite?",
            f"A loadout named '{quickname}' already exists. Overwrite it?",
        ):
            return
        save_api_loadout(db_path, quickname, url, payload_text, method)
        root.event_generate("<<LoadoutsUpdated>>")
        window.destroy()
        messagebox.showinfo("Saved", f"Loadout '{quickname}' saved.")

    ctk.CTkButton(window, text="Save", command=handle_save, font=control_font).pack(pady=field_pad)
    name_entry.focus_set()

def equip_saved_loadout():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    if api_url_var is None or api_request_text is None:
        messagebox.showerror("Error", "API panel is not ready yet.")
        return

    db_path = get_shoot_api_db_path(account_name)
    ensure_loadouts_table(db_path)
    loadouts = load_api_loadouts(db_path)
    if not loadouts:
        messagebox.showinfo("No Loadouts", "There are no saved loadouts yet.")
        return

    window = ctk.CTkToplevel(root)
    window.title("Loadouts")
    window.geometry("520x360")
    window.grab_set()
    bring_window_to_front(window)
    settings = get_settings()
    _, control_font = get_dialog_fonts(settings)
    tree_style = apply_dialog_treeview_style(settings)

    container = ctk.CTkFrame(window, fg_color="transparent")
    container.pack(fill="both", expand=True, padx=10, pady=10)

    tree = ttk.Treeview(
        container,
        columns=("quickname", "method", "url"),
        show="headings",
        style=tree_style,
    )
    tree.heading("quickname", text="Quickname")
    tree.heading("method", text="Method")
    tree.heading("url", text="URL")
    tree.column("quickname", width=160, anchor="w")
    tree.column("method", width=90, anchor="w")
    tree.column("url", width=250, anchor="w")
    tree.pack(fill="both", expand=True, pady=(0, 10))

    def refresh_tree():
        for item in tree.get_children():
            tree.delete(item)
        for quickname, method, url in load_api_loadouts(db_path):
            tree.insert("", "end", values=(quickname, method, url))

    loadouts_updated_id = root.bind(
        "<<LoadoutsUpdated>>",
        lambda _event: refresh_tree(),
        add="+",
    )

    def close_window():
        root.unbind("<<LoadoutsUpdated>>", loadouts_updated_id)
        window.destroy()

    def load_selected():
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("Select", "Choose a loadout to equip.")
            return
        quickname = tree.item(selected[0], "values")[0]
        loadout = fetch_api_loadout(db_path, quickname)
        if loadout is None:
            messagebox.showerror("Error", "Selected loadout not found.")
            return
        url, payload, method = loadout
        api_url_var.set(url)
        if method in API_METHODS:
            api_method_var.set(method)
        api_request_text.delete("1.0", END)
        if payload:
            api_request_text.insert("1.0", payload)
        global current_loadout_name
        current_loadout_name = quickname
        close_window()

    def handle_double_click(_event):
        load_selected()

    tree.bind("<Double-1>", handle_double_click)

    def open_editor(quickname: str = "", url: str = "", payload: str = "", method: str = "POST"):
        editor = ctk.CTkToplevel(window)
        editor.title("Loadout")
        editor.geometry("420x420")
        editor.grab_set()
        bring_window_to_front(editor)
        settings_local = get_settings()
        label_font, control_font_local = get_dialog_fonts(settings_local)
        label_pad, field_pad = get_dialog_spacing(settings_local)

        name_var = StringVar(value=quickname)
        url_var = StringVar(value=url)
        method_var = StringVar(value=method)

        ctk.CTkLabel(editor, text="Quickname", font=label_font).pack(
            anchor="w",
            padx=12,
            pady=(field_pad, label_pad),
        )
        name_entry = ctk.CTkEntry(editor, textvariable=name_var, font=control_font_local)
        name_entry.pack(fill="x", padx=12, pady=(0, field_pad))

        ctk.CTkLabel(editor, text="Method", font=label_font).pack(
            anchor="w",
            padx=12,
            pady=(0, label_pad),
        )
        method_menu = ctk.CTkOptionMenu(
            editor,
            values=list(API_METHODS),
            variable=method_var,
            font=control_font_local,
        )
        method_menu.pack(fill="x", padx=12, pady=(0, field_pad))

        ctk.CTkLabel(editor, text="URL", font=label_font).pack(
            anchor="w",
            padx=12,
            pady=(0, label_pad),
        )
        url_entry = ctk.CTkEntry(editor, textvariable=url_var, font=control_font_local)
        url_entry.pack(fill="x", padx=12, pady=(0, field_pad))

        ctk.CTkLabel(editor, text="Payload (JSON)", font=label_font).pack(
            anchor="w",
            padx=12,
            pady=(0, label_pad),
        )
        payload_box = ctk.CTkTextbox(editor, height=120, font=control_font_local)
        payload_box.pack(fill="both", padx=12, pady=(0, field_pad), expand=True)
        if payload:
            payload_box.insert("1.0", payload)

        def save():
            new_name = name_var.get().strip()
            new_url = url_var.get().strip()
            new_method = method_var.get().strip().upper()
            new_payload = payload_box.get("1.0", END).strip()
            if not new_name:
                messagebox.showerror("Error", "Quickname is required.")
                return
            if not new_url:
                messagebox.showerror("Error", "URL is required.")
                return
            if new_method not in API_METHODS:
                messagebox.showerror("Error", "Select a valid HTTP method.")
                return
            if new_payload:
                try:
                    json.loads(new_payload)
                except json.JSONDecodeError as exc:
                    messagebox.showerror("Invalid JSON", f"JSON body is invalid:\n{exc}")
                    return
            if new_name != quickname and fetch_api_loadout(db_path, new_name):
                if not messagebox.askyesno(
                    "Overwrite?",
                    f"A loadout named '{new_name}' already exists. Overwrite it?",
                ):
                    return
            save_api_loadout(db_path, new_name, new_url, new_payload, new_method)
            if quickname and new_name != quickname:
                delete_api_loadout(db_path, quickname)
            editor.destroy()
            refresh_tree()
            root.event_generate("<<LoadoutsUpdated>>")

        ctk.CTkButton(editor, text="Save", command=save, font=control_font_local).pack(
            pady=field_pad
        )
        name_entry.focus_set()

    def handle_add():
        current_method = (api_method_var.get() or "POST").strip().upper()
        current_url = (api_url_var.get() or "").strip()
        current_payload = api_request_text.get("1.0", END).strip()
        open_editor(
            method=current_method if current_method in API_METHODS else "POST",
            url=current_url,
            payload=current_payload,
        )

    def handle_edit():
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("Select", "Choose a loadout to edit.")
            return
        values = tree.item(selected[0], "values")
        loadout = fetch_api_loadout(db_path, values[0])
        if loadout is None:
            messagebox.showerror("Error", "Selected loadout not found.")
            return
        url, payload, method = loadout
        open_editor(values[0], url, payload, method)

    def handle_delete():
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("Select", "Choose a loadout to delete.")
            return
        quickname = tree.item(selected[0], "values")[0]
        if not messagebox.askyesno("Delete", f"Delete loadout '{quickname}'?"):
            return
        delete_api_loadout(db_path, quickname)
        refresh_tree()
        root.event_generate("<<LoadoutsUpdated>>")

    button_row = ctk.CTkFrame(container, fg_color="transparent")
    button_row.pack(fill="x")
    ctk.CTkButton(button_row, text="Add", command=handle_add, width=100, font=control_font).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(
        button_row, text="Edit", command=handle_edit, width=100, font=control_font
    ).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(
        button_row, text="Delete", command=handle_delete, width=100, font=control_font
    ).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(
        button_row, text="Load", command=load_selected, width=100, font=control_font
    ).pack(
        side="right"
    )

    io_row = ctk.CTkFrame(container, fg_color="transparent")
    io_row.pack(fill="x", pady=(6, 0))
    ctk.CTkButton(io_row, text="Import", command=import_loadouts_config, width=100, font=control_font).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(io_row, text="Export", command=export_loadouts_config, width=100, font=control_font).pack(
        side="left"
    )

    refresh_tree()
    window.protocol("WM_DELETE_WINDOW", close_window)

def open_response_mapping_dialog(selection_text: str):
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_response_mappings_table(db_path)

    variables = list(load_variables(db_path).keys())
    if not variables:
        messagebox.showwarning("No variables", "Create a variable first.")
        return
    loadouts = load_api_loadouts(db_path)
    if not loadouts:
        messagebox.showwarning("No loadouts", "Save a loadout before mapping responses.")
        return

    response_text = api_response_text.get("1.0", "end-1c") if api_response_text is not None else ""
    suggested_path = find_json_path_for_selection(response_text, selection_text) or ""

    window = ctk.CTkToplevel(root)
    window.title("Add response mapping")
    window.geometry("480x320")
    window.grab_set()
    bring_window_to_front(window)
    settings = get_settings()
    label_font, control_font = get_dialog_fonts(settings)
    label_pad, field_pad = get_dialog_spacing(settings)

    preview_text = selection_text.strip().replace("\n", " ")
    if len(preview_text) > 120:
        preview_text = preview_text[:117] + "..."

    ctk.CTkLabel(window, text="Selected response value", font=label_font).pack(
        anchor="w",
        padx=12,
        pady=(field_pad, label_pad),
    )
    ctk.CTkLabel(
        window,
        text=preview_text or "(empty selection)",
        font=control_font,
        wraplength=440,
        justify="left",
    ).pack(anchor="w", padx=12, pady=(0, field_pad))

    variable_var = StringVar(value=variables[0])
    ctk.CTkLabel(window, text="Variable", font=label_font).pack(
        anchor="w",
        padx=12,
        pady=(0, label_pad),
    )
    ctk.CTkOptionMenu(
        window,
        values=variables,
        variable=variable_var,
        font=control_font,
    ).pack(fill="x", padx=12, pady=(0, field_pad))

    loadout_names = [quickname for quickname, _, _ in loadouts]
    loadout_var = StringVar(value=loadout_names[0])
    ctk.CTkLabel(window, text="Loadout", font=label_font).pack(
        anchor="w",
        padx=12,
        pady=(0, label_pad),
    )
    ctk.CTkOptionMenu(
        window,
        values=loadout_names,
        variable=loadout_var,
        font=control_font,
    ).pack(fill="x", padx=12, pady=(0, field_pad))

    path_var = StringVar(value=suggested_path)
    ctk.CTkLabel(window, text="JSON path", font=label_font).pack(
        anchor="w",
        padx=12,
        pady=(0, label_pad),
    )
    path_entry = ctk.CTkEntry(window, textvariable=path_var, font=control_font)
    path_entry.pack(fill="x", padx=12, pady=(0, field_pad))

    def handle_save():
        variable_name = variable_var.get().strip()
        loadout_name = loadout_var.get().strip()
        json_path = path_var.get().strip()
        if not variable_name or not loadout_name:
            messagebox.showerror("Error", "Select both a variable and a loadout.")
            return
        if not json_path:
            messagebox.showerror("Error", "Provide a JSON path to save.")
            return
        try:
            parse_json_path(json_path)
        except ValueError as exc:
            messagebox.showerror("Invalid JSON path", f"JSON path is invalid:\n{exc}")
            return
        save_response_mapping(db_path, variable_name, loadout_name, json_path)
        window.destroy()
        messagebox.showinfo("Saved", "Response mapping saved.")
        root.event_generate("<<ResponseMappingsUpdated>>")

    ctk.CTkButton(window, text="Save", command=handle_save, font=control_font).pack(
        pady=(0, field_pad)
    )
    path_entry.focus_set()

def clear_response_panel():
    if api_response_text is None:
        return
    api_response_text.configure(state="normal")
    api_response_text.delete("1.0", END)
    api_response_text.configure(state="disabled")
    update_response_meta("")

def append_response_log(message: str):
    if api_response_text is None:
        return
    api_response_text.configure(state="normal")
    _insert_with_links(api_response_text, message, suffix="\n\n")
    api_response_text.see("end")
    api_response_text.configure(state="disabled")

def format_response_value(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)

def update_response_meta(response_text: str) -> None:
    if response_meta_details_var is None or response_meta_icon_label is None:
        return
    response_meta_details_var.set("")
    response_meta_icon_label.configure(text="")
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return
    response = payload.get("response", {})
    if not isinstance(response, dict) or "metaData" not in response:
        return
    meta = response.get("metaData")
    if not isinstance(meta, dict):
        return

    results_available = meta.get("resultsAvailable")
    last_result = meta.get("lastResult")
    details = f"Results available: {format_response_value(results_available)}"
    if last_result is not None:
        details += f" | Last: {format_response_value(last_result)}"
    response_meta_details_var.set(details)

    more_pages = meta.get("morePagesAvailable")
    if more_pages is True:
        response_meta_icon_label.configure(text="✓", text_color="green")
    elif more_pages is False:
        response_meta_icon_label.configure(text="✕", text_color="red")

def apply_response_variable_mappings(db_path: str, loadout_name: str, response_text: str) -> list[str]:
    if not loadout_name:
        return []
    mappings = load_response_mappings_for_loadout(db_path, loadout_name)
    if not mappings:
        return []
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return []
    updated = []
    for variable_name, json_path in mappings:
        try:
            value = get_value_at_json_path(payload, json_path)
        except (KeyError, ValueError, TypeError):
            continue
        save_variable(db_path, variable_name, format_response_value(value))
        updated.append(variable_name)
    return updated

def save_run_log():
    if not run_response_log:
        messagebox.showwarning("No log", "There is no run log to save yet.")
        return
    file_path = filedialog.asksaveasfilename(
        title="Save Run Log",
        defaultextension=".txt",
        filetypes=[("Text files", "*.txt")],
    )
    if not file_path:
        return
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write("\n\n".join(run_response_log))
    messagebox.showinfo("Saved", f"Run log saved to:\n{file_path}")
    append_log(f"📝 Run log saved to {file_path}")

def save_failed_log():
    if not failed_response_log:
        messagebox.showwarning("No failures", "There are no failed responses to save yet.")
        return
    file_path = filedialog.asksaveasfilename(
        title="Save Failed Responses Log",
        defaultextension=".txt",
        filetypes=[("Text files", "*.txt")],
    )
    if not file_path:
        return
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write("\n\n".join(failed_response_log))
    messagebox.showinfo("Saved", f"Failed responses log saved to:\n{file_path}")
    append_log(f"📝 Failed responses log saved to {file_path}")

def _open_export_path(path_text: str) -> None:
    normalized = os.path.expanduser(os.path.expandvars(path_text))
    if not os.path.isabs(normalized):
        normalized = os.path.abspath(normalized)
    try:
        webbrowser.open(Path(normalized).as_uri())
    except Exception:
        webbrowser.open(normalized)

def _insert_with_links(text_widget: ctk.CTkTextbox, message: str, *, suffix: str = "\n") -> None:
    global LOG_LINK_COUNTER
    tk_text = getattr(text_widget, "_textbox", text_widget)
    cursor = 0
    for match in LOG_LINK_PATTERN.finditer(message):
        if match.start() > cursor:
            tk_text.insert("end", message[cursor:match.start()])
        path_text = match.group(0)
        tag_name = f"log_link_{LOG_LINK_COUNTER}"
        LOG_LINK_COUNTER += 1
        tk_text.insert("end", path_text, tag_name)
        tk_text.tag_configure(tag_name, foreground="#1e6fff", underline=True)
        tk_text.tag_bind(
            tag_name,
            "<Button-1>",
            lambda event, p=path_text: _open_export_path(p),
        )
        tk_text.tag_bind(
            tag_name,
            "<Enter>",
            lambda event, w=tk_text: w.configure(cursor="hand2"),
        )
        tk_text.tag_bind(
            tag_name,
            "<Leave>",
            lambda event, w=tk_text: w.configure(cursor=""),
        )
        cursor = match.end()
    if cursor < len(message):
        tk_text.insert("end", message[cursor:])
    tk_text.insert("end", suffix)

def _shoot_api_table_count(conn: sqlite3.Connection, table: str) -> int:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
    except sqlite3.OperationalError:
        return 0
    row = cur.fetchone()
    return row[0] if row else 0

def _shoot_api_has_data(db_path: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            "variables",
            "variables_data",
            "response_variable_mappings",
            "api_loadouts",
            "api_chains",
            "api_chain_loadouts",
        ]
        return any(_shoot_api_table_count(conn, table) > 0 for table in tables)
    finally:
        conn.close()

def _fetch_source_rows(conn: sqlite3.Connection, query: str) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(query)
    except sqlite3.OperationalError:
        return []
    return cur.fetchall()

def _fetch_variables_data(conn: sqlite3.Connection) -> tuple[list[str], list[tuple]]:
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA table_info(variables_data)")
    except sqlite3.OperationalError:
        return [], []
    columns = [row[1] for row in cur.fetchall() if row[1] != "id"]
    if not columns:
        return [], []
    column_list = ", ".join(f'"{name.replace(chr(34), chr(34) * 2)}"' for name in columns)
    try:
        cur.execute(f"SELECT {column_list} FROM variables_data ORDER BY id")
    except sqlite3.OperationalError:
        return [], []
    return columns, cur.fetchall()

def _migrate_shoot_api_tables(account_name: str, target_db_path: str) -> None:
    source_db_path = get_data_db_path(account_name)
    if not os.path.exists(source_db_path):
        return

    ensure_variables_tables(target_db_path)
    ensure_response_mappings_table(target_db_path)
    ensure_loadouts_table(target_db_path)
    ensure_chain_tables(target_db_path)

    if _shoot_api_has_data(target_db_path):
        return

    source_conn = sqlite3.connect(source_db_path)
    try:
        variables_rows = _fetch_source_rows(source_conn, "SELECT name, value FROM variables")
        mappings_rows = _fetch_source_rows(
            source_conn,
            "SELECT variable_name, loadout_name, json_path FROM response_variable_mappings",
        )
        loadout_rows = _fetch_source_rows(
            source_conn,
            "SELECT quickname, url, payload, method FROM api_loadouts",
        )
        chain_rows = _fetch_source_rows(source_conn, "SELECT quickname FROM api_chains")
        chain_loadout_rows = _fetch_source_rows(
            source_conn,
            "SELECT chain_name, loadout_name, run_order FROM api_chain_loadouts",
        )
        chain_state = _fetch_source_rows(
            source_conn,
            "SELECT active_chain FROM api_chain_state WHERE id = 1",
        )
        data_columns, data_rows = _fetch_variables_data(source_conn)
    finally:
        source_conn.close()

    target_conn = sqlite3.connect(target_db_path)
    try:
        cur = target_conn.cursor()
        if variables_rows:
            cur.executemany(
                "INSERT OR REPLACE INTO variables (name, value) VALUES (?, ?)",
                variables_rows,
            )
        if mappings_rows:
            cur.executemany(
                """
                INSERT OR REPLACE INTO response_variable_mappings (
                    variable_name, loadout_name, json_path
                ) VALUES (?, ?, ?)
                """,
                mappings_rows,
            )
        if loadout_rows:
            cur.executemany(
                """
                INSERT OR REPLACE INTO api_loadouts (quickname, url, payload, method)
                VALUES (?, ?, ?, ?)
                """,
                loadout_rows,
            )
        if chain_rows:
            cur.executemany(
                "INSERT OR REPLACE INTO api_chains (quickname) VALUES (?)",
                chain_rows,
            )
        if chain_loadout_rows:
            cur.executemany(
                """
                INSERT OR REPLACE INTO api_chain_loadouts (chain_name, loadout_name, run_order)
                VALUES (?, ?, ?)
                """,
                chain_loadout_rows,
            )
        if chain_state:
            cur.execute(
                "UPDATE api_chain_state SET active_chain = ? WHERE id = 1",
                (chain_state[0][0],),
            )
        target_conn.commit()
    finally:
        target_conn.close()

    if data_columns and data_rows:
        create_variables_data_table(target_db_path, data_columns)
        data_dicts = [dict(zip(data_columns, row, strict=False)) for row in data_rows]
        insert_variables_rows(target_db_path, data_columns, data_dicts)

def get_shoot_api_db_path(account_name: str) -> str:
    db_path = get_credentials_db_path()
    _migrate_shoot_api_tables(account_name, db_path)
    return db_path

def _build_shoot_api_export_payload(
    db_path: str,
    *,
    variables: list[str] | None = None,
    loadouts: list[str] | None = None,
    chains: list[str] | None = None,
) -> dict:
    payload = {"version": 1, "variables": [], "loadouts": [], "chains": []}

    variable_map = load_variables(db_path)
    selected_variables = variables if variables is not None else list(variable_map.keys())
    for name in selected_variables:
        if name in variable_map:
            payload["variables"].append({"name": name, "value": variable_map.get(name, "")})

    if loadouts is None:
        loadouts = [name for name, _, _ in load_api_loadouts(db_path)]
    for name in loadouts:
        loadout = fetch_api_loadout(db_path, name)
        if not loadout:
            continue
        url, payload_text, method = loadout
        payload["loadouts"].append(
            {
                "quickname": name,
                "url": url,
                "payload": payload_text or "",
                "method": method,
            }
        )

    if chains is None:
        chains = load_chain_names(db_path)
    for name in chains:
        payload["chains"].append(
            {
                "name": name,
                "loadouts": load_chain_loadouts(db_path, name),
            }
        )

    return payload

def _load_shoot_api_export_payload(file_path: str) -> dict | None:
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        messagebox.showerror("Error", f"Failed to read export file:\n{exc}")
        return None
    if not isinstance(data, dict):
        messagebox.showerror("Error", "Export file must contain a JSON object.")
        return None
    return data

def export_variables_config():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_variables_tables(db_path)
    variables = load_variables(db_path)
    if not variables:
        messagebox.showwarning("No variables", "There are no variables to export.")
        return
    file_path = filedialog.asksaveasfilename(
        title="Export Variables",
        defaultextension=".json",
        filetypes=[("JSON files", "*.json")],
        initialfile="shoot_api_variables.json",
    )
    if not file_path:
        return
    payload = _build_shoot_api_export_payload(
        db_path,
        variables=list(variables.keys()),
        loadouts=[],
        chains=[],
    )
    try:
        with open(file_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except Exception as exc:
        messagebox.showerror("Error", f"Failed to export variables:\n{exc}")
        return
    messagebox.showinfo("Exported", f"Exported {len(variables)} variables.")

def import_variables_config():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_variables_tables(db_path)
    file_path = filedialog.askopenfilename(
        title="Import Variables",
        filetypes=[("JSON files", "*.json")],
    )
    if not file_path:
        return
    data = _load_shoot_api_export_payload(file_path)
    if data is None:
        return
    variables = data.get("variables", [])
    if not isinstance(variables, list):
        messagebox.showerror("Error", "Variables list is invalid.")
        return
    if not variables:
        messagebox.showwarning("No variables", "No variables found in the import file.")
        return
    imported = 0
    skipped = 0
    for entry in variables:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            skipped += 1
            continue
        value = entry.get("value", "")
        save_variable(db_path, name, "" if value is None else str(value))
        imported += 1
    if imported:
        root.event_generate("<<VariablesUpdated>>")
    message = f"Imported {imported} variables."
    if skipped:
        message += f" Skipped {skipped} invalid entries."
    messagebox.showinfo("Imported", message)

def export_loadouts_config():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_loadouts_table(db_path)
    ensure_chain_tables(db_path)
    loadout_names = [name for name, _, _ in load_api_loadouts(db_path)]
    chain_names = load_chain_names(db_path)
    if not loadout_names and not chain_names:
        messagebox.showwarning("No data", "There are no loadouts or chains to export.")
        return

    picker = ctk.CTkToplevel(root)
    picker.title("Export Loadouts")
    picker.geometry("520x520")
    picker.grab_set()
    bring_window_to_front(picker)
    settings = get_settings()
    label_font, control_font = get_dialog_fonts(settings)
    label_pad, field_pad = get_dialog_spacing(settings)

    ctk.CTkLabel(picker, text="Select loadouts to export:", font=label_font).pack(
        anchor="w", padx=12, pady=(field_pad, label_pad)
    )
    loadout_frame = ctk.CTkScrollableFrame(picker, height=180)
    loadout_frame.pack(fill="both", expand=True, padx=12, pady=(0, field_pad))

    loadout_vars = {}
    for name in loadout_names:
        var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(loadout_frame, text=name, variable=var, font=control_font).pack(
            anchor="w", pady=2
        )
        loadout_vars[name] = var

    ctk.CTkLabel(picker, text="Select chains to export:", font=label_font).pack(
        anchor="w", padx=12, pady=(0, label_pad)
    )
    chain_frame = ctk.CTkScrollableFrame(picker, height=180)
    chain_frame.pack(fill="both", expand=True, padx=12, pady=(0, field_pad))

    chain_vars = {}
    for name in chain_names:
        var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(chain_frame, text=name, variable=var, font=control_font).pack(
            anchor="w", pady=2
        )
        chain_vars[name] = var

    def handle_export():
        selected_loadouts = [name for name, var in loadout_vars.items() if var.get()]
        selected_chains = [name for name, var in chain_vars.items() if var.get()]
        if not selected_loadouts and not selected_chains:
            messagebox.showwarning("No selection", "Select at least one loadout or chain.")
            return
        file_path = filedialog.asksaveasfilename(
            title="Export Loadouts",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile="shoot_api_loadouts.json",
        )
        if not file_path:
            return
        payload = _build_shoot_api_export_payload(
            db_path,
            variables=[],
            loadouts=selected_loadouts,
            chains=selected_chains,
        )
        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to export loadouts:\n{exc}")
            return
        messagebox.showinfo(
            "Exported",
            f"Exported {len(selected_loadouts)} loadouts and {len(selected_chains)} chains.",
        )
        picker.destroy()

    button_row = ctk.CTkFrame(picker, fg_color="transparent")
    button_row.pack(fill="x", padx=12, pady=(0, field_pad))
    ctk.CTkButton(button_row, text="Export", command=handle_export, font=control_font).pack(
        side="right"
    )
    ctk.CTkButton(button_row, text="Cancel", command=picker.destroy, font=control_font).pack(
        side="right", padx=(0, 6)
    )

def import_loadouts_config():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_loadouts_table(db_path)
    ensure_chain_tables(db_path)
    file_path = filedialog.askopenfilename(
        title="Import Loadouts",
        filetypes=[("JSON files", "*.json")],
    )
    if not file_path:
        return
    data = _load_shoot_api_export_payload(file_path)
    if data is None:
        return
    loadouts = data.get("loadouts", [])
    chains = data.get("chains", [])
    if not isinstance(loadouts, list) or not isinstance(chains, list):
        messagebox.showerror("Error", "Loadouts or chains list is invalid.")
        return
    if not loadouts and not chains:
        messagebox.showwarning("No data", "No loadouts or chains found in the import file.")
        return
    imported_loadouts = 0
    imported_chains = 0
    skipped_loadouts = 0
    skipped_chains = 0
    for entry in loadouts:
        if not isinstance(entry, dict):
            skipped_loadouts += 1
            continue
        quickname = str(entry.get("quickname", "")).strip()
        url = str(entry.get("url", "")).strip()
        method = str(entry.get("method", "POST")).strip().upper()
        payload_text = entry.get("payload", "")
        if not quickname or not url or method not in API_METHODS:
            skipped_loadouts += 1
            continue
        save_api_loadout(db_path, quickname, url, str(payload_text or ""), method)
        imported_loadouts += 1
    for entry in chains:
        if not isinstance(entry, dict):
            skipped_chains += 1
            continue
        name = str(entry.get("name", "")).strip()
        loadout_sequence = entry.get("loadouts", [])
        if not name or not isinstance(loadout_sequence, list):
            skipped_chains += 1
            continue
        sequence = [str(item).strip() for item in loadout_sequence if str(item).strip()]
        if not sequence:
            skipped_chains += 1
            continue
        save_chain(db_path, name, sequence)
        imported_chains += 1

    if imported_loadouts:
        root.event_generate("<<LoadoutsUpdated>>")

    message = f"Imported {imported_loadouts} loadouts and {imported_chains} chains."
    if skipped_loadouts or skipped_chains:
        message += f" Skipped {skipped_loadouts + skipped_chains} invalid entries."
    messagebox.showinfo("Imported", message)

def open_variables_manager():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_variables_tables(db_path)
    ensure_response_mappings_table(db_path)

    window = ctk.CTkToplevel(root)
    window.title("Variables")
    window.geometry("520x360")
    window.grab_set()
    bring_window_to_front(window)
    settings = get_settings()
    _, control_font = get_dialog_fonts(settings)
    tree_style = apply_dialog_treeview_style(settings)

    container = ctk.CTkFrame(window, fg_color="transparent")
    container.pack(fill="both", expand=True, padx=10, pady=10)

    tree = ttk.Treeview(
        container,
        columns=("name", "value", "mapping"),
        show="headings",
        style=tree_style,
    )
    tree.heading("name", text="Name")
    tree.heading("value", text="Value")
    tree.heading("mapping", text="Mappings")
    tree.column("name", width=160, anchor="w")
    tree.column("value", width=300, anchor="w")
    tree.column("mapping", width=80, anchor="center")
    tree.pack(fill="both", expand=True, pady=(0, 10))

    def refresh_tree():
        for item in tree.get_children():
            tree.delete(item)
        mapping_summary = load_response_mapping_summary(db_path)
        for name, value in load_variables(db_path).items():
            mapping_count = mapping_summary.get(name, 0)
            mapping_label = f"Mapped x{mapping_count}" if mapping_count else ""
            tree.insert("", "end", values=(name, value, mapping_label))

    variables_updated_id = root.bind(
        "<<VariablesUpdated>>",
        lambda _event: refresh_tree(),
        add="+",
    )
    mappings_updated_id = root.bind(
        "<<ResponseMappingsUpdated>>",
        lambda _event: refresh_tree(),
        add="+",
    )

    def close_window():
        root.unbind("<<VariablesUpdated>>", variables_updated_id)
        root.unbind("<<ResponseMappingsUpdated>>", mappings_updated_id)
        window.destroy()

    def open_editor(name: str = "", value: str = ""):
        editor = ctk.CTkToplevel(window)
        editor.title("Variable")
        editor.geometry("420x320")
        editor.grab_set()
        bring_window_to_front(editor)
        settings_local = get_settings()
        label_font, control_font_local = get_dialog_fonts(settings_local)
        label_pad, field_pad = get_dialog_spacing(settings_local)

        name_var = StringVar(value=name)
        value_var = StringVar(value=value)

        ctk.CTkLabel(editor, text="Name", font=label_font).pack(
            anchor="w",
            padx=12,
            pady=(field_pad, label_pad),
        )
        name_entry = ctk.CTkEntry(editor, textvariable=name_var, font=control_font_local)
        name_entry.pack(fill="x", padx=12, pady=(0, field_pad))

        ctk.CTkLabel(editor, text="Value", font=label_font).pack(
            anchor="w",
            padx=12,
            pady=(0, label_pad),
        )
        value_entry = ctk.CTkEntry(editor, textvariable=value_var, font=control_font_local)
        value_entry.pack(fill="x", padx=12, pady=(0, field_pad))

        ctk.CTkLabel(editor, text="Response mappings", font=label_font).pack(
            anchor="w",
            padx=12,
            pady=(0, label_pad),
        )
        mapping_container = ctk.CTkFrame(editor, fg_color="transparent")
        mapping_container.pack(fill="x", padx=12, pady=(0, field_pad))
        mapping_container.grid_columnconfigure(0, weight=1)

        def render_mappings():
            for child in mapping_container.winfo_children():
                child.destroy()
            mappings = load_response_mappings_for_variable(db_path, name) if name else []
            if not mappings:
                ctk.CTkLabel(
                    mapping_container,
                    text="No response mappings yet.",
                    font=control_font_local,
                    text_color=("gray40", "gray70"),
                ).pack(anchor="w")
                return

            mapping_dict = {loadout_name: json_path for loadout_name, json_path in mappings}
            loadout_names = list(mapping_dict.keys())
            loadout_var = StringVar(value=loadout_names[0])
            path_var = StringVar()

            def update_path(*_args):
                path_var.set(mapping_dict.get(loadout_var.get(), ""))

            row = ctk.CTkFrame(mapping_container, fg_color="transparent")
            row.pack(fill="x")
            row.grid_columnconfigure(1, weight=1)

            loadout_menu = ctk.CTkOptionMenu(
                row,
                values=loadout_names,
                variable=loadout_var,
                font=control_font_local,
                width=140,
            )
            loadout_menu.grid(row=0, column=0, sticky="w")
            path_entry = ctk.CTkEntry(
                row,
                textvariable=path_var,
                font=control_font_local,
            )
            path_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
            path_entry.configure(state="readonly")

            def handle_delete_mapping():
                loadout_name = loadout_var.get().strip()
                if not loadout_name:
                    return
                if not messagebox.askyesno(
                    "Delete mapping",
                    f"Delete mapping for loadout '{loadout_name}'?",
                ):
                    return
                delete_response_mapping(db_path, name, loadout_name)
                render_mappings()
                refresh_tree()
                root.event_generate("<<ResponseMappingsUpdated>>")

            delete_button = ctk.CTkButton(
                row,
                text="Delete",
                command=handle_delete_mapping,
                font=control_font_local,
                width=80,
            )
            delete_button.grid(row=0, column=2, padx=(8, 0))

            loadout_var.trace_add("write", update_path)
            update_path()

        render_mappings()

        def save():
            new_name = name_var.get().strip()
            new_value = value_var.get().strip()
            if not new_name:
                messagebox.showerror("Error", "Variable name is required.")
                return
            save_variable(db_path, new_name, new_value)
            editor.destroy()
            refresh_tree()
            root.event_generate("<<VariablesUpdated>>")

        ctk.CTkButton(editor, text="Save", command=save, font=control_font_local).pack(pady=field_pad)
        name_entry.focus_set()

    def handle_add():
        open_editor()

    def handle_edit():
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("Select", "Choose a variable to edit.")
            return
        values = tree.item(selected[0], "values")
        open_editor(values[0], values[1])

    def handle_delete():
        selected = tree.selection()
        if not selected:
            messagebox.showwarning("Select", "Choose a variable to delete.")
            return
        name = tree.item(selected[0], "values")[0]
        if not messagebox.askyesno("Delete", f"Delete variable '{name}'?"):
            return
        delete_variable(db_path, name)
        refresh_tree()
        root.event_generate("<<VariablesUpdated>>")

    button_row = ctk.CTkFrame(container, fg_color="transparent")
    button_row.pack(fill="x")
    ctk.CTkButton(button_row, text="Add", command=handle_add, width=100, font=control_font).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(button_row, text="Edit", command=handle_edit, width=100, font=control_font).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(button_row, text="Delete", command=handle_delete, width=100, font=control_font).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(button_row, text="Import", command=import_variables_config, width=100, font=control_font).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(button_row, text="Export", command=export_variables_config, width=100, font=control_font).pack(
        side="left",
        padx=(0, 6),
    )
    ctk.CTkButton(button_row, text="Close", command=close_window, width=100, font=control_font).pack(
        side="right"
    )

    refresh_tree()
    window.protocol("WM_DELETE_WINDOW", close_window)

def refresh_retry_207_button_visibility() -> None:
    if retry_207_button is None:
        return
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        retry_207_button.grid_remove()
        return

    try:
        db_path = get_shoot_api_db_path(account_name)
        rows = load_variables_data(db_path)
    except Exception:
        retry_207_button.grid_remove()
        return

    has_207_rows = any(str(row.get(PROCESSED_COLUMN) or "").strip() == "2" for row in rows)
    if has_207_rows:
        retry_207_button.grid(row=9, column=0, sticky="w", pady=2)
    else:
        retry_207_button.grid_remove()


def import_variables_csv():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_variables_tables(db_path)

    file_path = select_csv_for_validation("Import Variables CSV")
    if not file_path:
        return

    try:
        with open_csv(file_path) as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except Exception as exc:
        messagebox.showerror("Error", f"Failed to read CSV:\n{exc}")
        return

    if not rows:
        messagebox.showwarning("No data", "The CSV file contains no rows.")
        return

    source_columns = reader.fieldnames or []
    columns = [
        col
        for col in source_columns
        if col not in {PROCESSED_COLUMN, PROCESSED_AT_COLUMN, PROCESSED_DETAILS_COLUMN}
    ]
    columns.extend([PROCESSED_COLUMN, PROCESSED_AT_COLUMN, PROCESSED_DETAILS_COLUMN])

    normalized_rows = []
    for row in rows:
        normalized = {
            key: row.get(key)
            for key in source_columns
            if key not in {PROCESSED_COLUMN, PROCESSED_AT_COLUMN, PROCESSED_DETAILS_COLUMN}
        }
        processed_value = str(row.get(PROCESSED_COLUMN, "")).strip()
        normalized[PROCESSED_COLUMN] = processed_value if processed_value in {"1", "2"} else "0"
        normalized[PROCESSED_AT_COLUMN] = (
            row.get(PROCESSED_AT_COLUMN)
            if normalized[PROCESSED_COLUMN] in {"1", "2"}
            else ""
        )
        normalized[PROCESSED_DETAILS_COLUMN] = (
            row.get(PROCESSED_DETAILS_COLUMN)
            if normalized[PROCESSED_COLUMN] in {"1", "2"}
            else ""
        )
        normalized_rows.append(normalized)

    create_variables_data_table(db_path, columns)
    insert_variables_rows(db_path, columns, normalized_rows)

    messagebox.showinfo("Imported", f"Loaded {len(rows)} rows into variables_data with processing status.")
    refresh_retry_207_button_visibility()

def run_variables_csv(*, retry_including_207: bool = False):
    if send_api_button is None:
        return

    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            root.after(0, lambda: messagebox.showerror("Error", "Choose an Active account first (top-right)."))
            return

        try:
            creds = fetch_credentials(account_name)
        except Exception as exc:
            root.after(0, lambda: messagebox.showerror("Error", f"Unable to load credentials:\n{exc}"))
            return

        db_path = get_shoot_api_db_path(account_name)
        data_rows = load_variables_data_with_ids(db_path)
        if not data_rows:
            root.after(0, lambda: messagebox.showerror("Error", "Import a variables CSV first."))
            return

        method = (api_method_var.get() or "").strip().upper()
        if method not in API_METHODS:
            root.after(0, lambda: messagebox.showerror("Error", "Select a valid HTTP method."))
            return

        raw_url_template = api_url_var.get().strip()
        payload_template = api_request_text.get("1.0", END).strip() if api_request_text is not None else ""

        headers = {
            "brightpearl-app-ref": creds.app_ref,
            "brightpearl-account-token": creds.token,
        }

        total_rows = len(data_rows)
        pending_rows = [
            row
            for row in data_rows
            if not is_row_processed(row, treat_207_as_processed=not retry_including_207)
        ]
        pending_total = len(pending_rows)
        processed_rows = 0
        cancelled = False
        failed_rows = []
        csv_headers = sorted({key for row in data_rows for key in row.keys() if key != "id"})
        run_response_log.clear()
        failed_response_log.clear()
        loadout_name = current_loadout_name
        variables_updated = False

        def start_progress():
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            mode_label = "retry (including 207)" if retry_including_207 else "run"
            api_status_var.set(
                f"Running 0/{pending_total} pending rows (from {total_rows} total) [{mode_label}]..."
            )
            send_api_button.configure(state="disabled")
            if retry_207_button is not None:
                retry_207_button.configure(state="disabled")
            if save_log_button is not None:
                save_log_button.grid_remove()
            if save_failed_log_button is not None:
                save_failed_log_button.grid_remove()
            clear_response_panel()

        if pending_total == 0:
            msg = "No rows eligible for retry including 207." if retry_including_207 else "All rows are already processed (1 or 2)."

            def show_nothing_to_run():
                refresh_retry_207_button_visibility()
                messagebox.showinfo("Nothing to run", msg)
                refresh_retry_207_button_visibility()

            root.after(0, show_nothing_to_run)
            return

        root.after(0, start_progress)

        for idx, row in enumerate(pending_rows, start=1):
            if cancel_token.is_set():
                cancelled = True
                break

            row_values = {key: value for key, value in row.items() if key != "id"}
            resolved_url = replace_template_vars(raw_url_template, row_values)
            full_url = normalize_api_url(resolved_url, account_name, creds.region)
            if not full_url:
                root.after(0, lambda: messagebox.showerror("Error", "Enter an API URL."))
                break

            payload_text = replace_template_vars(payload_template, row_values)
            json_payload, payload_error = parse_json_payload(payload_text)

            response_text = ""
            status_text = ""
            next_throttle = 0
            remaining = None
            status_code = None
            response_headers = ""
            error_detail = ""
            if payload_error is not None:
                response_text = f"JSON body is invalid:\n{payload_error}"
                error_detail = str(payload_error)
                status_text = f"Pending row {idx}/{pending_total} | Invalid JSON."
            else:
                api_response = execute_api_request(
                    method,
                    full_url,
                    headers,
                    json_payload,
                    timeout=30,
                )
                status_code = api_response.status_code
                response_text = api_response.response_text
                response_headers = api_response.response_headers
                next_throttle = api_response.next_throttle
                remaining = api_response.remaining
                error_detail = api_response.error_detail
                if status_code is None:
                    status_text = f"Pending row {idx}/{pending_total} | Request failed."
                else:
                    status_text = f"Pending row {idx}/{pending_total} | Status: {status_code}"
                if remaining is not None:
                    status_text += f" | Remaining: {remaining}"
                if next_throttle > 0:
                    status_text += f" | Throttle: {next_throttle}ms"
                updated_vars = apply_response_variable_mappings(
                    db_path,
                    loadout_name,
                    response_text,
                )
                if updated_vars:
                    variables_updated = True

            is_failure = status_code is None or not is_processed_success_status(status_code)

            if not is_failure and row.get("id") is not None:
                mark_variables_row_processed(db_path, int(row["id"]), status_code, response_text)

            if is_failure:
                failure_entry = (
                    f"Pending row {idx}/{pending_total} | Status {status_code}\n"
                    f"URL: {full_url}\n"
                    f"Payload: {payload_text or ''}\n"
                    f"Response Headers:\n{response_headers or ''}\n"
                    f"Response Body:\n{response_text}\n"
                )
                if error_detail:
                    failure_entry += f"Error Detail:\n{error_detail}\n"
                failed_response_log.append(failure_entry)

            def update_ui():
                log_entry = f"Pending row {idx}/{pending_total} | Status {status_code}\n{response_text}"
                if not is_failure:
                    run_response_log.append(log_entry)
                append_response_log(log_entry)
                update_response_meta(response_text)
                api_status_var.set(status_text)
                progress_bar.set(idx / max(1, pending_total))
                if status_code is not None:
                    append_log(f"📥 Row {idx} response (Status {status_code}): {response_text}")
                else:
                    append_log(f"📥 Row {idx} response: {response_text}")

            root.after(0, update_ui)

            processed_rows = idx
            if is_failure:
                failed_row = {key: row_values.get(key) for key in csv_headers}
                failed_row["status_code"] = status_code if status_code is not None else ""
                failed_rows.append(failed_row)
                if payload_error is not None:
                    break
            sleep_ms = throttle_sleep_ms(remaining, next_throttle)
            if sleep_ms > 0:
                sleep_with_cancel_ms(sleep_ms, cancel_token=cancel_token)

        def finish():
            progress_stop_and_reset_to_determinate()
            send_api_button.configure(state="normal")
            if retry_207_button is not None:
                retry_207_button.configure(state="normal")
            refresh_retry_207_button_visibility()
            if failed_rows:
                os.makedirs("output", exist_ok=True)
                filename = os.path.join(
                    "output",
                    f"{account_name}_shoot_api_failures_{int(time.time())}.csv",
                )
                with open(filename, "w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=[*csv_headers, "status_code"])
                    writer.writeheader()
                    writer.writerows(failed_rows)
                append_log(f"⚠️ {len(failed_rows)} failed rows exported to {filename}")
            if save_log_button is not None and run_response_log:
                save_log_button.grid()
            if save_failed_log_button is not None and failed_response_log:
                save_failed_log_button.grid()
            if cancelled:
                messagebox.showinfo(
                    "Run cancelled",
                    f"Run cancelled after {processed_rows}/{pending_total} pending rows.",
                )
            else:
                messagebox.showinfo(
                    "Run complete",
                    f"Processed {processed_rows}/{pending_total} pending rows. 200 => processed=1, 207 => processed=2.",
                )
            refresh_retry_207_button_visibility()
            if variables_updated:
                root.event_generate("<<VariablesUpdated>>")

        root.after(0, finish)

    threading.Thread(target=task, daemon=True).start()

def open_chain_loadouts_dialog():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return
    db_path = get_shoot_api_db_path(account_name)
    ensure_chain_tables(db_path)
    ensure_loadouts_table(db_path)

    loadouts = [name for name, _, _ in load_api_loadouts(db_path)]
    if not loadouts:
        messagebox.showwarning("No Loadouts", "Save at least one loadout first.")
        return

    window = ctk.CTkToplevel(root)
    window.title("Chain Loadouts")
    window.geometry("560x420")
    window.grab_set()
    bring_window_to_front(window)
    settings = get_settings()
    label_font, control_font = get_dialog_fonts(settings)
    label_pad, field_pad = get_dialog_spacing(settings)

    chain_names = load_chain_names(db_path)
    active_chain = get_active_chain(db_path) or ""

    ctk.CTkLabel(window, text="Chain", font=label_font).pack(
        anchor="w", padx=12, pady=(field_pad, label_pad)
    )
    chain_name_var = StringVar(value=active_chain if active_chain else "")
    chain_entry = ctk.CTkEntry(window, textvariable=chain_name_var, font=control_font)
    chain_entry.pack(fill="x", padx=12, pady=(0, field_pad))

    if chain_names:
        ctk.CTkLabel(window, text="Existing chains", font=label_font).pack(
            anchor="w", padx=12, pady=(0, label_pad)
        )
        chain_select_var = StringVar(value=active_chain if active_chain else chain_names[0])
        chain_menu = ctk.CTkOptionMenu(
            window,
            values=chain_names,
            variable=chain_select_var,
            font=control_font,
        )
        chain_menu.pack(fill="x", padx=12, pady=(0, field_pad))
    else:
        chain_select_var = None
        chain_menu = None

    list_frame = ctk.CTkFrame(window, fg_color="transparent")
    list_frame.pack(fill="both", expand=True, padx=12, pady=(0, field_pad))
    list_frame.grid_columnconfigure(0, weight=1)
    list_frame.grid_rowconfigure(0, weight=1)

    chain_list = tk.Listbox(list_frame, height=8, selectmode=tk.SINGLE)
    chain_list.grid(row=0, column=0, sticky="nsew")
    chain_scroll = ctk.CTkScrollbar(list_frame, orientation=VERTICAL, command=chain_list.yview)
    chain_scroll.grid(row=0, column=1, sticky="ns")
    chain_list.configure(yscrollcommand=chain_scroll.set)

    def refresh_chain_list(items: list[str]):
        chain_list.delete(0, END)
        for name in items:
            chain_list.insert(END, name)

    def load_chain(name: str):
        if not name:
            refresh_chain_list([])
            return
        chain_name_var.set(name)
        refresh_chain_list(load_chain_loadouts(db_path, name))

    if chain_select_var is not None:
        load_chain(chain_select_var.get())

        def handle_chain_select(*_args):
            load_chain(chain_select_var.get())

        chain_select_var.trace_add("write", handle_chain_select)

    def add_loadout():
        selector = ctk.CTkToplevel(window)
        selector.title("Add Loadout")
        selector.geometry("360x320")
        selector.grab_set()
        bring_window_to_front(selector)

        listbox = tk.Listbox(selector, selectmode=tk.SINGLE)
        listbox.pack(fill="both", expand=True, padx=12, pady=12)
        for name in loadouts:
            listbox.insert(END, name)

        def confirm():
            selection = listbox.curselection()
            if not selection:
                messagebox.showwarning("Select", "Choose a loadout.")
                return
            selected_name = listbox.get(selection[0])
            chain_list.insert(END, selected_name)
            selector.destroy()

        ctk.CTkButton(selector, text="Add", command=confirm, font=control_font).pack(
            pady=(0, field_pad)
        )

    def remove_loadout():
        selection = chain_list.curselection()
        if not selection:
            messagebox.showwarning("Select", "Choose a loadout to remove.")
            return
        chain_list.delete(selection[0])

    def move_up():
        selection = chain_list.curselection()
        if not selection or selection[0] == 0:
            return
        idx = selection[0]
        value = chain_list.get(idx)
        chain_list.delete(idx)
        chain_list.insert(idx - 1, value)
        chain_list.selection_set(idx - 1)

    def move_down():
        selection = chain_list.curselection()
        if not selection or selection[0] == chain_list.size() - 1:
            return
        idx = selection[0]
        value = chain_list.get(idx)
        chain_list.delete(idx)
        chain_list.insert(idx + 1, value)
        chain_list.selection_set(idx + 1)

    button_row = ctk.CTkFrame(window, fg_color="transparent")
    button_row.pack(fill="x", padx=12, pady=(0, field_pad))
    ctk.CTkButton(button_row, text="Add", command=add_loadout, font=control_font).pack(
        side="left", padx=(0, 6)
    )
    ctk.CTkButton(
        button_row, text="Remove", command=remove_loadout, font=control_font
    ).pack(side="left", padx=(0, 6))
    ctk.CTkButton(button_row, text="Up", command=move_up, font=control_font).pack(
        side="left", padx=(0, 6)
    )
    ctk.CTkButton(button_row, text="Down", command=move_down, font=control_font).pack(
        side="left"
    )

    active_var = StringVar(value="1" if active_chain else "0")
    ctk.CTkCheckBox(
        window,
        text="Set as active chain",
        variable=active_var,
        onvalue="1",
        offvalue="0",
        font=control_font,
    ).pack(anchor="w", padx=12, pady=(0, field_pad))

    def save_chain_dialog():
        chain_name = chain_name_var.get().strip()
        if not chain_name:
            messagebox.showerror("Error", "Enter a chain quickname.")
            return
        loadout_sequence = list(chain_list.get(0, END))
        if not loadout_sequence:
            messagebox.showerror("Error", "Add at least one loadout.")
            return
        save_chain(db_path, chain_name, loadout_sequence)
        if active_var.get() == "1":
            set_active_chain(db_path, chain_name)
        messagebox.showinfo("Saved", f"Chain '{chain_name}' saved.")
        window.destroy()

    def delete_chain_dialog():
        name = chain_name_var.get().strip()
        if not name:
            messagebox.showwarning("Select", "Choose a chain to delete.")
            return
        if not messagebox.askyesno("Delete", f"Delete chain '{name}'?"):
            return
        delete_chain(db_path, name)
        chain_name_var.set("")
        refresh_chain_list([])
        messagebox.showinfo("Deleted", f"Chain '{name}' deleted.")

    action_row = ctk.CTkFrame(window, fg_color="transparent")
    action_row.pack(fill="x", padx=12, pady=(0, field_pad))
    ctk.CTkButton(action_row, text="Save Chain", command=save_chain_dialog, font=control_font).pack(
        side="right", padx=(6, 0)
    )
    ctk.CTkButton(action_row, text="Delete Chain", command=delete_chain_dialog, font=control_font).pack(
        side="right"
    )

def run_chain():
    if send_api_button is None:
        return

    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            root.after(0, lambda: messagebox.showerror("Error", "Choose an Active account first (top-right)."))
            return

        try:
            creds = fetch_credentials(account_name)
        except Exception as exc:
            root.after(0, lambda: messagebox.showerror("Error", f"Unable to load credentials:\n{exc}"))
            return

        db_path = get_shoot_api_db_path(account_name)
        ensure_chain_tables(db_path)
        chain_name = get_active_chain(db_path)
        if not chain_name:
            root.after(0, lambda: messagebox.showerror("Error", "No active chain selected."))
            return
        chain_loadouts = load_chain_loadouts(db_path, chain_name)
        if not chain_loadouts:
            root.after(0, lambda: messagebox.showerror("Error", "Active chain has no loadouts."))
            return

        data_rows = load_variables_data_with_ids(db_path)
        if not data_rows:
            root.after(0, lambda: messagebox.showerror("Error", "Import a variables CSV first."))
            return

        headers = {
            "brightpearl-app-ref": creds.app_ref,
            "brightpearl-account-token": creds.token,
        }

        total_steps = len(data_rows) * len(chain_loadouts)
        step_index = 0
        cancelled = False
        failed_rows = []
        run_response_log.clear()
        failed_response_log.clear()

        def start_progress():
            progress_bar.configure(mode="determinate")
            progress_bar.set(0)
            api_status_var.set(f"Running chain '{chain_name}' 0/{total_steps}...")
            send_api_button.configure(state="disabled")
            if retry_207_button is not None:
                retry_207_button.configure(state="disabled")
            if save_log_button is not None:
                save_log_button.grid_remove()
            if save_failed_log_button is not None:
                save_failed_log_button.grid_remove()
            clear_response_panel()

        root.after(0, start_progress)

        for row_index, row in enumerate(data_rows, start=1):
            if cancel_token.is_set():
                cancelled = True
                break
            for loadout_name in chain_loadouts:
                if cancel_token.is_set():
                    cancelled = True
                    break
                loadout = fetch_api_loadout(db_path, loadout_name)
                if loadout is None:
                    failed_response_log.append(
                        f"Row {row_index} | Loadout '{loadout_name}' not found."
                    )
                    continue
                url, payload_template, method = loadout
                variables = load_variables(db_path)
                merged_vars = {**variables, **row}
                resolved_url = replace_template_vars(url, merged_vars)
                full_url = normalize_api_url(resolved_url, account_name, creds.region)
                if not full_url:
                    failed_response_log.append(
                        f"Row {row_index} | Loadout '{loadout_name}' has empty URL."
                    )
                    continue

                payload_text = replace_template_vars(payload_template, merged_vars)
                json_payload, payload_error = parse_json_payload(payload_text)
                response_text = ""
                status_text = ""
                status_code = None
                response_headers = ""
                error_detail = ""
                next_throttle = 0
                remaining = None
                if payload_error is not None:
                    response_text = f"JSON body is invalid:\n{payload_error}"
                    error_detail = str(payload_error)
                    status_text = f"Row {row_index} | Loadout {loadout_name} | Invalid JSON."
                else:
                    api_response = execute_api_request(
                        method,
                        full_url,
                        headers,
                        json_payload,
                        timeout=30,
                    )
                    status_code = api_response.status_code
                    response_text = api_response.response_text
                    response_headers = api_response.response_headers
                    next_throttle = api_response.next_throttle
                    remaining = api_response.remaining
                    error_detail = api_response.error_detail
                    if status_code is None:
                        status_text = f"Row {row_index} | Loadout {loadout_name} | Request failed."
                    else:
                        status_text = f"Row {row_index} | Loadout {loadout_name} | Status {status_code}"
                    if remaining is not None:
                        status_text += f" | Remaining: {remaining}"
                    if next_throttle > 0:
                        status_text += f" | Throttle: {next_throttle}ms"
                    apply_response_variable_mappings(db_path, loadout_name, response_text)

                is_failure = status_code is None or not is_processed_success_status(status_code)
                if payload_error is not None:
                    is_failure = True

                if is_failure:
                    failure_entry = (
                        f"Row {row_index} | Loadout {loadout_name} | Status {status_code}\n"
                        f"URL: {full_url}\n"
                        f"Payload: {payload_text or ''}\n"
                        f"Response Headers:\n{response_headers or ''}\n"
                        f"Response Body:\n{response_text}\n"
                    )
                    if error_detail:
                        failure_entry += f"Error Detail:\n{error_detail}\n"
                    failed_response_log.append(failure_entry)

                step_index += 1
                progress_value = step_index / max(1, total_steps)

                def update_ui():
                    log_entry = (
                        f"Row {row_index} | Loadout {loadout_name} | Status {status_code}\n"
                        f"{response_text}"
                    )
                    if not is_failure:
                        run_response_log.append(log_entry)
                    append_response_log(log_entry)
                    update_response_meta(response_text)
                    api_status_var.set(status_text)
                    progress_bar.set(progress_value)
                    if status_code is not None:
                        append_log(
                            f"📥 Row {row_index} loadout {loadout_name} response "
                            f"(Status {status_code}): {response_text}"
                        )
                    else:
                        append_log(
                            f"📥 Row {row_index} loadout {loadout_name} response: {response_text}"
                        )

                root.after(0, update_ui)

                sleep_ms = throttle_sleep_ms(remaining, next_throttle)
                if sleep_ms > 0:
                    sleep_with_cancel_ms(sleep_ms, cancel_token=cancel_token)

        def finish():
            progress_stop_and_reset_to_determinate()
            send_api_button.configure(state="normal")
            if retry_207_button is not None:
                retry_207_button.configure(state="normal")
            refresh_retry_207_button_visibility()
            if failed_response_log:
                if save_failed_log_button is not None:
                    save_failed_log_button.grid()
            if run_response_log and save_log_button is not None:
                save_log_button.grid()
            if cancelled:
                messagebox.showinfo(
                    "Run cancelled",
                    f"Chain run cancelled after {step_index}/{total_steps} steps.",
                )
            else:
                messagebox.showinfo(
                    "Run complete",
                    f"Chain '{chain_name}' complete ({step_index}/{total_steps} steps).",
                )
            root.event_generate("<<VariablesUpdated>>")

        root.after(0, finish)

    threading.Thread(target=task, daemon=True).start()

# ---------------- Menus ----------------
def open_add_account_dialog():
    dlg = ctk.CTkToplevel(root)
    dlg.title("Register new account")
    dlg.resizable(False, False)
    dlg.geometry("400x260")
    dlg.transient(root)
    dlg.after_idle(lambda: bring_window_to_front(dlg))
    settings = get_settings()
    label_font, control_font = get_dialog_fonts(settings)
    label_pad, field_pad = get_dialog_spacing(settings)

    _acc = StringVar(dlg); _app = StringVar(dlg); _tok = StringVar(dlg); _reg = StringVar(dlg, value="euw1")

    ctk.CTkLabel(dlg, text="Account Name", font=label_font).grid(
        row=0,
        column=0,
        sticky="w",
        padx=6,
        pady=label_pad,
    )
    ctk.CTkEntry(dlg, textvariable=_acc, width=260, font=control_font).grid(
        row=0,
        column=1,
        padx=6,
        pady=field_pad,
    )

    ctk.CTkLabel(dlg, text="App Ref", font=label_font).grid(
        row=1,
        column=0,
        sticky="w",
        padx=6,
        pady=label_pad,
    )
    ctk.CTkEntry(dlg, textvariable=_app, width=260, font=control_font).grid(
        row=1,
        column=1,
        padx=6,
        pady=field_pad,
    )

    ctk.CTkLabel(dlg, text="Token", font=label_font).grid(
        row=2,
        column=0,
        sticky="w",
        padx=6,
        pady=label_pad,
    )
    ctk.CTkEntry(dlg, textvariable=_tok, width=260, show="*", font=control_font).grid(
        row=2,
        column=1,
        padx=6,
        pady=field_pad,
    )

    ctk.CTkLabel(dlg, text="Region", font=label_font).grid(
        row=3,
        column=0,
        sticky="w",
        padx=6,
        pady=label_pad,
    )
    cb = ctk.CTkComboBox(
        dlg,
        variable=_reg,
        values=("euw1", "use1"),
        width=120,
        state="readonly",
        font=control_font,
    )
    cb.grid(row=3, column=1, sticky="e", padx=6, pady=field_pad)

    credential_status_label = ctk.CTkLabel(dlg, text="", font=control_font, anchor="w")

    def _update_credential_status(is_valid: bool | None) -> None:
        if is_valid is None:
            credential_status_label.configure(text="", text_color=("gray10", "gray90"))
            register_button.configure(text="Register new account")
            return
        if is_valid:
            credential_status_label.configure(text="✓ Credentials verified", text_color="green")
            register_button.configure(text="Register new account")
        else:
            credential_status_label.configure(text="✕ Invalid credentials", text_color="red")
            register_button.configure(text="Invalid credentials")

    def _check_credentials():
        account_name = _acc.get().strip()
        app_ref = _app.get().strip()
        token = _tok.get().strip()
        region = _reg.get().strip()
        if not all([account_name, app_ref, token, region]):
            messagebox.showerror("Error", "Please fill in all fields.")
            return
        if region not in ("euw1", "use1"):
            messagebox.showerror("Error", "Region must be 'euw1' or 'use1'.")
            return

        _update_credential_status(None)
        check_button.configure(state="disabled")

        def task():
            url = (
                f"https://{region}.brightpearlconnect.com/public-api/{account_name}/"
                "integration-service/account-configuration"
            )
            headers = {
                "brightpearl-app-ref": app_ref,
                "brightpearl-account-token": token,
            }
            request_payload = {
                "method": "GET",
                "url": url,
                "headers": {
                    "brightpearl-app-ref": app_ref,
                    "brightpearl-account-token": f"***{token[-4:]}" if len(token) >= 4 else "***",
                },
            }
            try:
                log_payload(
                    "🔎 Add-account credential check request:\n"
                    f"{json.dumps(request_payload, indent=2, ensure_ascii=False)}",
                    log_callback,
                )
                response = requests.get(url, headers=headers, verify=False, timeout=30)
                is_valid = response.status_code == 200
                try:
                    response_body = json.dumps(response.json(), indent=2, ensure_ascii=False)
                except ValueError:
                    response_body = response.text
                log_payload(
                    "📥 Add-account credential check response:\n"
                    f"Status: {response.status_code}\n"
                    f"Headers: {dict(response.headers)}\n"
                    f"Body:\n{response_body}",
                    log_callback,
                )
            except requests.RequestException as exc:
                is_valid = False
                log_payload(
                    "❌ Add-account credential check request failed:\n"
                    f"{exc}",
                    log_callback,
                )

            def update_ui():
                _update_credential_status(is_valid)
                check_button.configure(state="normal")

            root.after(0, update_ui)

        threading.Thread(target=task, daemon=True).start()

    def _register():
        account_name = _acc.get().strip()
        app_ref = _app.get().strip()
        token = _tok.get().strip()
        region = _reg.get().strip()
        if not all([account_name, app_ref, token, region]):
            messagebox.showerror("Error", "Please fill in all fields.")
            return
        if region not in ("euw1","use1"):
            messagebox.showerror("Error", "Region must be 'euw1' or 'use1'.")
            return
        upsert_credentials(account_name, app_ref, token, region)
        refresh_accounts_combo()
        append_log(f"🧾 Registered account: {account_name}")
        messagebox.showinfo("Saved", "Credentials saved successfully.")

    button_row = ctk.CTkFrame(dlg, fg_color="transparent")
    button_row.grid(row=4, column=0, columnspan=2, pady=field_pad)

    check_button = ctk.CTkButton(
        button_row,
        text="Check",
        command=_check_credentials,
        width=30,
        font=control_font,
    )
    check_button.grid(row=0, column=0, padx=(0, 8))

    register_button = ctk.CTkButton(
        button_row,
        text="Register new account",
        command=_register,
        width=260,
        font=control_font,
    )
    register_button.grid(row=0, column=1, sticky='e', padx=(0, 8))

    credential_status_label.grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(0, field_pad))

def remove_active_account():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showwarning("No account", "No Active account selected.")
        return

    data_db_path = Path(get_data_db_path(account_name))
    confirm_message = (
        f"Remove account '{account_name}' from credentials and delete its local data file?\n\n"
        "Access and data will be permanently, non-recoverably removed. "
        "This cannot be undone."
    )
    if not messagebox.askyesno("Permanently remove account and data?", confirm_message, icon="warning"):
        return

    try:
        if data_db_path.exists():
            data_db_path.unlink()
            append_log(f"🗑️ Deleted local data file: {data_db_path}")
        else:
            append_log(f"ℹ️ No local data file found for account: {account_name}")

        with sqlite3.connect(get_credentials_db_path()) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM credentials WHERE account_name = ?", (account_name,))
            deleted_credentials = cur.rowcount

    except OSError as e:
        messagebox.showerror(
            "Removal failed",
            f"Could not delete the local data file for '{account_name}':\n{data_db_path}\n\n{e}",
        )
        append_log(f"❌ Failed to delete local data file for account {account_name}: {e}")
        return
    except sqlite3.Error as e:
        messagebox.showerror(
            "Removal failed",
            f"Deleted local data file, but could not remove credentials for '{account_name}':\n{e}",
        )
        append_log(f"❌ Failed to remove credentials for account {account_name}: {e}")
        return

    if deleted_credentials:
        append_log(f"🗑️ Removed account credentials: {account_name}")
    else:
        append_log(f"ℹ️ Account credentials were already absent: {account_name}")
    refresh_accounts_combo()
    messagebox.showinfo("Account removed", f"Account '{account_name}' and its local data have been removed.")

def show_about():
    messagebox.showinfo(
        "About",
        f"Brightpearl Pro Serv Multi Tool\n\nVersion {APP_VERSION}\n\nTaco Tim ™"
    )

def get_dialog_fonts(settings: AppSettings) -> tuple[ctk.CTkFont, ctk.CTkFont]:
    label_font = ctk.CTkFont(family="Segoe UI", size=settings.menu_font_size)
    control_font = ctk.CTkFont(family="Segoe UI", size=settings.submenu_font_size)
    return label_font, control_font

def get_dialog_spacing(settings: AppSettings) -> tuple[int, int]:
    label_pad = max(6, int(settings.menu_font_size * 0.6))
    field_pad = max(8, int(settings.submenu_font_size * 0.7))
    return label_pad, field_pad

def apply_dialog_treeview_style(settings: AppSettings, style_name: str = "Dialog.Treeview") -> str:
    style = ttk.Style()
    style.configure(style_name, font=("Segoe UI", settings.submenu_font_size))
    style.configure(f"{style_name}.Heading", font=("Segoe UI", settings.menu_font_size))
    style.configure(style_name, rowheight=max(20, settings.submenu_font_size + 10))
    return style_name

def bring_window_to_front(window: tk.Toplevel) -> None:
    window.lift()
    window.attributes("-topmost", True)
    window.after(0, lambda: window.attributes("-topmost", False))
    window.focus_force()

def apply_menu_fonts(settings: AppSettings) -> None:
    menu_font = tkfont.Font(family="Segoe UI", size=settings.menu_font_size)
    submenu_font = tkfont.Font(family="Segoe UI", size=settings.submenu_font_size)
    root.option_add("*Menu.font", menu_font)
    if menubar is not None:
        menubar.configure(font=menu_font)
    for menu in (
        file_menu,
        c_tools_menu,
        gl_tools_menu,
        mtn_tools_menu,
        export_menu,
        experimental_menu,
        help_menu,
        settings_menu,
    ):
        if menu is not None:
            menu.configure(font=submenu_font)


def apply_api_text_fonts(settings: AppSettings) -> None:
    request_font = ctk.CTkFont(family="Consolas", size=settings.json_font_size)
    response_font = ctk.CTkFont(family="Consolas", size=settings.response_font_size)
    if api_request_text is not None:
        api_request_text.configure(font=request_font)
    if api_response_text is not None:
        api_response_text.configure(font=response_font)


def apply_global_settings(settings: AppSettings) -> None:
    ctk.set_appearance_mode(settings.appearance_mode)
    ctk.set_default_color_theme(get_color_theme_name(settings))
    root.geometry(settings.start_resolution)
    apply_menu_fonts(settings)
    apply_api_text_fonts(settings)
    update_logo_for_theme(settings)


def open_settings_dialog():
    settings = get_settings()
    label_font, control_font = get_dialog_fonts(settings)
    label_pad, field_pad = get_dialog_spacing(settings)
    window = ctk.CTkToplevel(root)
    window.title("Global Settings")
    window.geometry("520x760")
    window.transient(root)
    window.grab_set()
    bring_window_to_front(window)

    form_frame = ctk.CTkFrame(window)
    form_frame.pack(fill="both", expand=True, padx=16, pady=16)
    form_frame.grid_columnconfigure(1, weight=1)

    log_level_var = tk.StringVar(value=settings.log_level)
    log_output_var = tk.StringVar(value=settings.log_output_dir)
    throttle_var = tk.StringVar(value=str(settings.throttle_threshold))
    menu_font_var = tk.StringVar(value=str(settings.menu_font_size))
    submenu_font_var = tk.StringVar(value=str(settings.submenu_font_size))
    response_font_var = tk.StringVar(value=str(settings.response_font_size))
    json_font_var = tk.StringVar(value=str(settings.json_font_size))
    resolution_var = tk.StringVar(value=settings.start_resolution)
    appearance_var = tk.StringVar(value=settings.appearance_mode)
    appearance_theme_var = tk.StringVar(value=settings.appearance_theme)
    unmatched_dir_var = tk.StringVar(value=settings.unmatched_output_dir)
    download_max_retries_var = tk.StringVar(value=str(settings.download_max_retries))
    download_default_sleep_ms_var = tk.StringVar(value=str(settings.download_default_sleep_ms))
    upload_max_retries_var = tk.StringVar(value=str(settings.upload_max_retries))
    upload_default_sleep_ms_var = tk.StringVar(value=str(settings.upload_default_sleep_ms))
    stock_correction_batch_size_var = tk.StringVar(value=str(settings.stock_correction_batch_size))

    fields = [
        ("Log level", log_level_var, ["PAYLOAD", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
        ("Log export folder", log_output_var, None),
        ("Throttle threshold", throttle_var, None),
        ("Menu font size", menu_font_var, None),
        ("Submenu font size", submenu_font_var, None),
        ("Response text font size", response_font_var, None),
        ("JSON/request text font size", json_font_var, None),
        ("Starting resolution (WxH)", resolution_var, None),
        ("Appearance mode", appearance_var, ["System", "Light", "Dark"]),
        ("Appearance theme", appearance_theme_var, ["Brightpearl", "Sage"]),
        ("Unmatched export folder", unmatched_dir_var, None),
        ("Download Max Retries", download_max_retries_var, None),
        ("Download Default Sleep m/s", download_default_sleep_ms_var, None),
        ("Upload Max Retries", upload_max_retries_var, None),
        ("Upload Default Sleep m/s", upload_default_sleep_ms_var, None),
        ("Stock correction batch size (1-500)", stock_correction_batch_size_var, None),
    ]

    for row_index, (label, variable, options) in enumerate(fields):
        ctk.CTkLabel(form_frame, text=label, font=label_font).grid(
            row=row_index,
            column=0,
            sticky="w",
            pady=label_pad,
        )
        if options:
            ctk.CTkOptionMenu(
                form_frame,
                values=options,
                variable=variable,
                font=control_font,
            ).grid(
                row=row_index,
                column=1,
                sticky="ew",
                padx=(12, 0),
                pady=field_pad,
            )
        else:
            ctk.CTkEntry(form_frame, textvariable=variable, font=control_font).grid(
                row=row_index,
                column=1,
                sticky="ew",
                padx=(12, 0),
                pady=field_pad,
            )

    def save_changes():
        raw = {
            "log_level": log_level_var.get(),
            "log_output_dir": log_output_var.get(),
            "throttle_threshold": throttle_var.get(),
            "menu_font_size": menu_font_var.get(),
            "submenu_font_size": submenu_font_var.get(),
            "response_font_size": response_font_var.get(),
            "json_font_size": json_font_var.get(),
            "start_resolution": resolution_var.get(),
            "appearance_mode": appearance_var.get(),
            "appearance_theme": appearance_theme_var.get(),
            "unmatched_output_dir": unmatched_dir_var.get(),
            "download_max_retries": download_max_retries_var.get(),
            "download_default_sleep_ms": download_default_sleep_ms_var.get(),
            "upload_max_retries": upload_max_retries_var.get(),
            "upload_default_sleep_ms": upload_default_sleep_ms_var.get(),
            "stock_correction_batch_size": stock_correction_batch_size_var.get(),
        }
        new_settings = normalize_settings(raw)
        save_settings(new_settings)
        apply_global_settings(new_settings)
        messagebox.showinfo("Settings saved", "Global settings have been updated.")
        window.destroy()

    actions = ctk.CTkFrame(window)
    actions.pack(fill="x", padx=16, pady=(0, field_pad))
    ctk.CTkButton(
        actions,
        text="Cancel",
        command=window.destroy,
        font=control_font,
    ).pack(side="right", padx=(8, 0))
    ctk.CTkButton(
        actions,
        text="Save settings",
        command=save_changes,
        font=control_font,
    ).pack(side="right")

def set_view(view_name: str):
    global current_tool_view
    current_tool_view = view_name
    render_left_panel()
    
def resource_path(filename: str) -> str:
    """Return absolute path for a file that lives in the *current working directory*."""
    return os.path.abspath(os.path.join(os.getcwd(), filename))

def find_image_path(filename: str = "sage.png") -> str:
    candidates = [
        os.path.abspath(os.path.join(os.getcwd(), filename)),
        os.path.abspath(os.path.join(os.path.dirname(__file__), filename)),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


def get_color_theme_name(settings: AppSettings) -> str:
    return "blue" if settings.appearance_theme == "Brightpearl" else "green"


def get_logo_filename(settings: AppSettings) -> str:
    return "brightpearl.png" if settings.appearance_theme == "Brightpearl" else "sage.png"


def update_logo_for_theme(settings: AppSettings) -> None:
    global logo_label_widget
    if logo_label_widget is None:
        return
    logo_path = find_image_path(get_logo_filename(settings))
    if logo_path:
        try:
            img = Image.open(logo_path)
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", Image.BICUBIC))
            w, h = img.size
            target_size = (max(1, w // 4), max(1, h // 4))
            img = img.resize(target_size, resample)
            photo = ctk.CTkImage(light_image=img, dark_image=img, size=target_size)
            logo_label_widget.configure(image=photo, text="")
            root._logo_photo = photo
            logo_label_widget._photo = photo
            return
        except Exception:
            pass
    logo_label_widget.configure(text=settings.appearance_theme, image=None, fg_color=("gray85", "gray25"), corner_radius=6)
    
# ---------------- GUI Setup ----------------
app_settings = get_settings()
ctk.set_appearance_mode(app_settings.appearance_mode)
ctk.set_default_color_theme(get_color_theme_name(app_settings))

root = ctk.CTk()
root.withdraw()
root.title("Brightpearl Pro Serv Multi Tool")
root.configure(fg_color=("white", "gray15"))
root.minsize(*MAIN_WINDOW_MIN_SIZE)
root.geometry(app_settings.start_resolution)

def show_main_window():
    """Show the main application window after startup UI has been built."""
    root.deiconify()
    root.state("normal")
    root.lift()
    root.focus_force()

class PerformanceGraph:
    def __init__(self, parent, *, width=360, height=80, window_seconds=60):
        self.window_seconds = window_seconds
        self.frame = ctk.CTkFrame(parent, corner_radius=10, fg_color=("gray95", "gray20"))
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        header.grid_columnconfigure(0, weight=1)

        self.value_var = StringVar(value="-- records/s")
        ctk.CTkLabel(
            header,
            text="Sync speed",
            font=("Segoe UI", 11, "bold"),
            text_color=("gray20", "gray90"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            textvariable=self.value_var,
            font=("Segoe UI", 10),
            text_color=("gray35", "gray70"),
        ).grid(row=0, column=1, sticky="e")

        bg_color = self.frame.cget("fg_color")
        if isinstance(bg_color, (tuple, list)):
            bg_color = bg_color[0]
        self.canvas = tk.Canvas(
            self.frame,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=bg_color,
        )
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.canvas.bind("<Configure>", self._handle_resize)

        self.points = []
        self.width = width
        self.height = height

    def _handle_resize(self, event):
        self.width = max(event.width, 1)
        self.height = max(event.height, 1)
        self._render()

    def add_point(self, rate_per_sec: float, timestamp: float):
        self.points.append((timestamp, rate_per_sec))
        cutoff = timestamp - self.window_seconds
        self.points = [(ts, rate) for ts, rate in self.points if ts >= cutoff]
        self.value_var.set(f"{rate_per_sec:,.0f} records/s")
        self._render()

    def _render(self):
        self.canvas.delete("all")
        left = 36
        right = self.width - 10
        top = 6
        bottom = self.height - 18
        axis_color = "#9aa0a6"
        grid_color = "#cbd5e1"
        line_color = "#2563eb"
        self.canvas.create_line(left, top, left, bottom, fill=axis_color, width=1)
        self.canvas.create_line(left, bottom, right, bottom, fill=axis_color, width=1)

        for i in range(1, 3):
            y = top + ((bottom - top) / 3) * i
            self.canvas.create_line(left, y, right, y, fill=grid_color, width=1, dash=(2, 3))

        self.canvas.create_text(
            left + 4, top, text="records/s", anchor="nw", fill=axis_color, font=("Segoe UI", 8)
        )
        self.canvas.create_text(
            right - 20, bottom + 2, text="time", anchor="ne", fill=axis_color, font=("Segoe UI", 8)
        )

        if not self.points:
            self.canvas.create_text(
                (left + right) / 2,
                (top + bottom) / 2,
                text="Waiting for data…",
                fill="#94a3b8",
                font=("Segoe UI", 9),
            )
            return
        max_rate = max(rate for _, rate in self.points) or 1
        window_start = self.points[-1][0] - self.window_seconds
        coords = []
        for ts, rate in self.points:
            x = left + ((ts - window_start) / self.window_seconds) * (right - left)
            y = bottom - (rate / max_rate) * (bottom - top)
            coords.extend([x, y])
        if len(coords) >= 4:
            self.canvas.create_line(*coords, fill=line_color, width=2, smooth=True)
        else:
            self.canvas.create_oval(
                coords[0] - 2,
                coords[1] - 2,
                coords[0] + 2,
                coords[1] + 2,
                fill=line_color,
                outline=line_color,
            )


def apply_menu_mode_labels(compact: bool):
    if menubar is None:
        return
    compact_map = {
        "Configuration Tools": "Config",
        "Go Live Tools": "Go Live",
        "Maintenance Tools": "Maint",
        "Settings": "Prefs",
        "Experimental": "Labs",
    }
    for index, full_label in menu_label_defaults.items():
        label = compact_map.get(full_label, full_label) if compact else full_label
        menubar.entryconfig(index, label=label)

def apply_logo_layout(compact: bool) -> None:
    if logo_label_widget is None:
        return
    if compact:
        logo_label_widget.grid_configure(column=1, sticky="nw", padx=(10, 2), pady=4)
    else:
        logo_label_widget.grid_configure(column=0, sticky="nw", padx=2, pady=4)


def apply_ui_mode():
    global compact_mode_enabled, full_mode_geometry
    was_compact = compact_mode_enabled
    compact_mode_enabled = bool(ui_mode_compact_var.get()) if ui_mode_compact_var is not None else False
    apply_menu_mode_labels(compact_mode_enabled)

    if compact_mode_enabled:
        if not was_compact:
            full_mode_geometry = root.geometry()
            root.geometry("360x550")
        root.minsize(360, 520)
        apply_logo_layout(True)
        if performance_graph is not None:
            performance_graph.frame.grid_remove()
        if progress_bar is not None:
            progress_bar.grid_remove()
        if compact_progress_label is not None:
            compact_progress_label.grid(row=0, column=0, sticky="e")
        if current_tool_view == "shoot_apis":
            show_right_panel("shoot_apis")
            if compact_placeholder_label is not None:
                compact_placeholder_label.grid_remove()
        else:
            if log_frame is not None:
                log_frame.grid_remove()
            if shoot_api_frame is not None:
                shoot_api_frame.grid_remove()
            if compact_placeholder_label is not None:
                compact_placeholder_label.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
    else:
        if was_compact and full_mode_geometry:
            root.geometry(full_mode_geometry)
        root.minsize(*MAIN_WINDOW_MIN_SIZE)
        apply_logo_layout(False)
        if performance_graph is not None:
            performance_graph.frame.grid(row=0, column=1, columnspan=2, sticky="nsew", padx=6, pady=(4, 0))
        if progress_bar is not None:
            progress_bar.grid(row=0, column=0, sticky="ew")
        if compact_progress_label is not None:
            compact_progress_label.grid_remove()
        if compact_placeholder_label is not None:
            compact_placeholder_label.grid_remove()
        show_right_panel("shoot_apis" if current_tool_view == "shoot_apis" else "log")

def build_main_ui():
    global menubar, file_menu, settings_menu, c_tools_menu, gl_tools_menu, mtn_tools_menu, export_menu, experimental_menu, help_menu
    global left_frame, right_frame, bottom_frame, log_viewer, scrollbar, cancel_btn, progress_bar
    global account_name_var, app_ref_var, token_var, region_var, selected_account_var, accounts_combo
    global contact_catalogue_append_var, contact_catalogue_first_result_var
    global order_catalogue_append_var, order_catalogue_first_result_var
    global address_contact_lookup_mode_var
    global log_frame, shoot_api_frame, performance_graph, logo_label_widget
    global ui_mode_compact_var, compact_progress_var, compact_progress_label, compact_placeholder_label, menu_label_defaults

    menubar = tk.Menu(root, tearoff=0)
    root.config(menu=menubar)
    file_menu = tk.Menu(menubar, tearoff=0)
    file_menu.add_command(label="Add a new account…", command=open_add_account_dialog)
    file_menu.add_command(label="Remove active account", command=remove_active_account)
    file_menu.add_separator()
    file_menu.add_command(label="Quit", command=root.quit)
    menubar.add_cascade(label="File", menu=file_menu)
    file_menu_idx = menubar.index("end")

    settings_menu = tk.Menu(menubar, tearoff=0)
    settings_menu.add_command(label="Global Settings…", command=open_settings_dialog)
    ui_mode_compact_var = tk.BooleanVar(master=root, value=False)
    settings_menu.add_checkbutton(label="Compact UI mode", variable=ui_mode_compact_var, command=apply_ui_mode)
    menubar.add_cascade(label="Settings", menu=settings_menu)
    settings_menu_idx = menubar.index("end")
    
    c_tools_menu = tk.Menu(menubar, tearoff=0)
    c_tools_menu.add_command(label="Contact Import", command=lambda: set_view("contact_import"))
    c_tools_menu.add_command(label="Product Import", command=lambda: set_view("product_import"))
    c_tools_menu.add_command(label="Custom Fields", command=lambda: set_view("custom_fields"))
    c_tools_menu.add_command(label="Multiple Addresses", command=lambda: set_view("additional_addresses"))
    c_tools_menu.add_command(label="Warehouse Locations", command=lambda: set_view("warehouse_locations"))
    c_tools_menu.add_command(label="Warehouse Zones", command=lambda: set_view("warehouse_zones"))
    menubar.add_cascade(label="Configuration Tools", menu=c_tools_menu)
    c_tools_menu_idx = menubar.index("end")

    gl_tools_menu = tk.Menu(menubar, tearoff=0)
    gl_tools_menu.add_command(label="Inventory Import", command=lambda: set_view("inventory_import"))
    gl_tools_menu.add_command(label="Open Sales", command=lambda: set_view("open_sales"))
    gl_tools_menu.add_command(label="Open Purchases", command=lambda: set_view("open_purchases"))
    gl_tools_menu.add_command(label="Historic Sales", command=lambda: set_view("historic_sales"))
    menubar.add_cascade(label="Go Live Tools", menu=gl_tools_menu)
    gl_tools_menu_idx = menubar.index("end")

    mtn_tools_menu = tk.Menu(menubar, tearoff=0)
    mtn_tools_menu.add_command(label="Forget Contact (GDPR)", command=lambda: set_view("forget_contact"))
    mtn_tools_menu.add_command(
        label="Warehouse Service Maintenance",
        command=lambda: set_view("warehouse_service_maintenance"),
    )
    menubar.add_cascade(label="Maintenance Tools", menu=mtn_tools_menu)
    mtn_tools_menu_idx = menubar.index("end")

    export_menu = tk.Menu(menubar, tearoff=0)
    export_menu.add_command(
        label="Product Catalogue",
        command=lambda: set_view("export_product_catalogue"),
    )
    export_menu.add_command(
        label="IP Stock History",
        command=lambda: set_view("export_ip_stock_history"),
    )
    menubar.add_cascade(label="Export", menu=export_menu)
    export_menu_idx = menubar.index("end")

    experimental_menu = tk.Menu(menubar, tearoff=0)
    experimental_menu.add_command(label="SYNC", command=lambda: set_view("experimental_sync"))
    experimental_menu.add_command(label="Shoot APIs", command=lambda: set_view("shoot_apis"))
    experimental_menu.add_command(label="IC Training Helper", command=lambda: set_view("ic_training_helper"))
    menubar.add_cascade(label="Experimental", menu=experimental_menu)
    experimental_menu_idx = menubar.index("end")

    help_menu = tk.Menu(menubar, tearoff=0)
    help_menu.add_command(label="About", command=show_about)
    menubar.add_cascade(label="Help", menu=help_menu)
    help_menu_idx = menubar.index("end")
    menu_label_defaults = {
        file_menu_idx: "File",
        settings_menu_idx: "Settings",
        c_tools_menu_idx: "Configuration Tools",
        gl_tools_menu_idx: "Go Live Tools",
        mtn_tools_menu_idx: "Maintenance Tools",
        export_menu_idx: "Export",
        help_menu_idx: "Help",
    }
    menu_label_defaults[experimental_menu_idx] = "Experimental"

    apply_menu_fonts(get_settings())

    logo_path = find_image_path(get_logo_filename(get_settings()))
    print(f"[logo] CWD={os.getcwd()}")
    print(f"[logo] __file__ dir={os.path.dirname(__file__)}")
    print(f"[logo] Resolved path={logo_path} | exists={bool(logo_path and os.path.exists(logo_path))}")

    if logo_path:
        try:
            img = Image.open(logo_path)
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", Image.BICUBIC))
            w, h = img.size
            target_size = (max(1, w // 4), max(1, h // 4))
            img = img.resize(target_size, resample)
            photo = ctk.CTkImage(light_image=img, dark_image=img, size=target_size)
            print(f"[logo] Loaded with Pillow, size after resize={target_size}")

            logo_label_widget = ctk.CTkLabel(root, image=photo, text="")
            root._logo_photo = photo
            logo_label_widget._photo = photo
            logo_label_widget.grid(row=0, column=0, rowspan=2, sticky="nw", padx=2, pady=4)

        except Exception as e:
            print(f"[logo] Failed to load image: {e}")
            logo_label_widget = ctk.CTkLabel(
                root, text=get_settings().appearance_theme, fg_color=("gray85", "gray25"), corner_radius=6
            )
            logo_label_widget.grid(row=0, column=0, rowspan=2, sticky="w", padx=4, pady=4)
    else:
        print(f"[logo] {get_logo_filename(get_settings())} not found in CWD or script directory.")
        logo_label_widget = ctk.CTkLabel(
            root, text=get_settings().appearance_theme, fg_color=("gray85", "gray25"), corner_radius=6
        )
        logo_label_widget.grid(row=0, column=0, rowspan=2, sticky="nw", padx=0, pady=0)

    performance_graph = PerformanceGraph(root)
    performance_graph.frame.grid(row=0, column=1, columnspan=2, sticky="nsew", padx=6, pady=(4, 0))

    ctk.CTkLabel(root, text="Account name").grid(row=0, column=0, sticky="sw", padx=0)
    selected_account_var = StringVar(master=root)
    contact_catalogue_append_var = StringVar(master=root, value="No")
    contact_catalogue_first_result_var = StringVar(master=root, value="")
    address_contact_lookup_mode_var = StringVar(master=root, value="email")
    order_catalogue_append_var = StringVar(master=root, value="No")
    order_catalogue_first_result_var = StringVar(master=root, value="")
    accounts_combo = ctk.CTkComboBox(
        root, variable=selected_account_var, values=[], width=140, state="readonly"
    )
    accounts_combo.grid(row=0, column=0, padx=6, sticky="se")

    left_frame = ctk.CTkFrame(root)
    left_frame.grid(row=2, column=0, columnspan=1, sticky="nsew", padx=4, pady=4)

    right_frame = ctk.CTkFrame(root)
    right_frame.grid(row=2, column=1, columnspan=4, sticky="nsew", padx=4, pady=4)

    bottom_left_frame = ctk.CTkFrame(root, fg_color="transparent")
    bottom_left_frame.grid(row=3, column=0, sticky="nsew", padx=(4, 0), pady=6)

    bottom_frame = ctk.CTkFrame(root, fg_color="transparent")
    bottom_frame.grid(row=3, column=1, columnspan=4, sticky="nsew", padx=(0, 4), pady=6)

    root.grid_columnconfigure(1, weight=1)
    root.grid_columnconfigure(2, weight=1)
    root.grid_rowconfigure(2, weight=1)

    right_frame.grid_columnconfigure(0, weight=1)
    right_frame.grid_rowconfigure(0, weight=1)

    log_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
    log_frame.grid(row=0, column=0, sticky="nsew")
    log_frame.grid_columnconfigure(0, weight=1)
    log_frame.grid_rowconfigure(0, weight=1)

    log_viewer = ctk.CTkTextbox(log_frame, height=360, width=520, wrap="word")
    log_viewer.grid(row=0, column=0, sticky="nsew")
    scrollbar = ctk.CTkScrollbar(log_frame, orientation=VERTICAL, command=log_viewer.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    log_viewer.configure(yscrollcommand=scrollbar.set)

    shoot_api_frame = None
    compact_placeholder_label = ctk.CTkLabel(
        right_frame,
        text="Cristina mode \nenabled.\nProgress % of \nEACH \nSUB-SYNC \nFUNCTION \nshown at the \nbottom right.\n\n\n\nThe counter may \njust start over \nand over, \nwait for the \npop up!",
        font=("Segoe UI", 12, "bold"),
        text_color=("gray40", "gray75"),
        justify="center",
    )

    log_viewer.configure(state="normal")
    log_viewer.insert("end", "Add your Brightpearl accounts from: File, Add a new account. You can add as many accounts as you like.\n\nMake sure you select the correct 'Active account' before you process any actions.\n\nSync your Brightpearl data with this tool so we can validate the data in your CSV. \n\nUse the CSV templates available here <---\n\nUpload your CSV, we'll validate it against the data we just synced, you'll find csv files of all the exceptions saved in the same place as this tool.\n\nEach time you upload a new CSV we will forget anything previously uploaded.\n\nGOOD LUCK!\n")
    log_viewer.configure(state="disabled")

    cancel_btn = ctk.CTkButton(
        bottom_left_frame,
        text="Cancel Operation",
        fg_color="#f3a6b5",
        hover_color="#f06d82",
        text_color="black",
        command=cancel_task,
        width=200,
    )
    cancel_btn.grid(row=0, column=0, sticky="w")

    progress_bar = ctk.CTkProgressBar(bottom_frame, orientation="horizontal", mode="determinate")
    progress_bar.set(0)
    progress_bar.grid(row=0, column=0, sticky="ew")
    compact_progress_var = StringVar(value="0%")
    compact_progress_label = ctk.CTkLabel(
        bottom_frame,
        textvariable=compact_progress_var,
        font=("Segoe UI", 16, "bold"),
        text_color=("gray20", "gray90"),
    )
    bottom_frame.grid_columnconfigure(0, weight=1)

    account_name_var = StringVar(value="")
    app_ref_var = StringVar(value="")
    token_var = StringVar(value="")
    region_var = StringVar()

    def handle_performance_update(rate_per_sec: float, timestamp: float):
        if performance_graph is None:
            return
        root.after(0, lambda: performance_graph.add_point(rate_per_sec, timestamp))

    set_performance_callback(handle_performance_update)
    apply_ui_mode()

# ---------------- Logging ----------------
def append_log(message):
    log_viewer.configure(state="normal")
    _insert_with_links(log_viewer, message, suffix="\n")
    log_viewer.see(END)
    log_viewer.configure(state="disabled")

def log_callback(message):
    if apply_progress_from_message(message):
        return
    append_log(message)

# ---------------- Left panel renderer ----------------
def clear_left_panel():
    for child in left_frame.winfo_children():
        child.destroy()

def show_right_panel(panel_name: str):
    if compact_mode_enabled and panel_name != "shoot_apis":
        if log_frame is not None:
            log_frame.grid_remove()
        if shoot_api_frame is not None:
            shoot_api_frame.grid_remove()
        if compact_placeholder_label is not None:
            compact_placeholder_label.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        return

    if panel_name == "ic_training_helper":
        if log_frame is not None:
            log_frame.grid_remove()
        if shoot_api_frame is not None:
            shoot_api_frame.grid_remove()
        if compact_placeholder_label is not None:
            compact_placeholder_label.grid_remove()
        return

    if log_frame is not None:
        log_frame.grid_remove()
    if shoot_api_frame is not None:
        shoot_api_frame.grid_remove()
    if compact_placeholder_label is not None:
        compact_placeholder_label.grid_remove()
    for child in right_frame.winfo_children():
        if child not in (log_frame, shoot_api_frame, compact_placeholder_label):
            child.destroy()

    if panel_name == "shoot_apis":
        render_shoot_apis_panel()
        if shoot_api_frame is not None:
            shoot_api_frame.grid(row=0, column=0, sticky="nsew")
    else:
        if log_frame is not None:
            log_frame.grid(row=0, column=0, sticky="nsew")

def render_shoot_apis_view():
    clear_left_panel()

    global send_api_button, retry_207_button, api_method_var
    if api_method_var is None:
        api_method_var = StringVar(master=root, value=API_METHODS[0])
    ctk.CTkLabel(left_frame, text="Shoot APIs", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )

    method_label = ctk.CTkLabel(left_frame, text="HTTP Method")
    method_label.grid(row=1, column=0, sticky="w", pady=(0, 2))

    method_dropdown = ctk.CTkComboBox(
        left_frame,
        variable=api_method_var,
        values=API_METHODS,
        state="readonly",
        width=220,
    )
    method_dropdown.grid(row=2, column=0, sticky="w", pady=(0, 6))


    ctk.CTkButton(
        left_frame,
        text="Variables",
        width=220,
        command=open_variables_manager,
    ).grid(row=6, column=0, sticky="w", pady=2)

    ctk.CTkButton(
        left_frame,
        text="Import CSV",
        width=220,
        command=import_variables_csv,
    ).grid(row=7, column=0, sticky="w", pady=2)

    ctk.CTkButton(
        left_frame,
        text="Run",
        width=220,
        command=run_variables_csv,
    ).grid(row=8, column=0, sticky="w", pady=2)

    retry_207_button = ctk.CTkButton(
        left_frame,
        text="Retry including 207",
        width=220,
        command=lambda: run_variables_csv(retry_including_207=True),
    )

    refresh_retry_207_button_visibility()

    ctk.CTkButton(
        left_frame,
        text="Chain Loadouts",
        width=220,
        command=open_chain_loadouts_dialog,
    ).grid(row=13, column=0, sticky="w", pady=2)

    ctk.CTkButton(
        left_frame,
        text="Run Chain",
        width=220,
        command=run_chain,
    ).grid(row=14, column=0, sticky="w", pady=2)

    ctk.CTkButton(
        left_frame,
        text="Save Loadout",
        width=220,
        command=save_current_api_loadout,
    ).grid(row=11, column=0, sticky="w", pady=2)

    ctk.CTkButton(
        left_frame,
        text="Equip Loadout",
        width=220,
        command=equip_saved_loadout,
    ).grid(row=12, column=0, sticky="w", pady=2)
    
    ctk.CTkLabel(left_frame, text="").grid(row=10, column=0, pady=10)


    send_api_button = ctk.CTkButton(
        left_frame,
        text="SEND IT ONCE",
        width=220,
        command=send_api_request,
    )
    send_api_button.grid(row=3, column=0, sticky="w", pady=2)


def render_ic_training_helper_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="IC Training Helper", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(
        left_frame,
        text="Reference Data",
        width=220,
        command=render_ic_training_reference_panel,
    ).grid(row=1, column=0, sticky="w", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Sales Orders",
        width=220,
        command=render_ic_training_sales_orders_panel,
    ).grid(row=2, column=0, sticky="w", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Inventory",
        width=220,
        command=render_ic_training_inventory_panel,
    ).grid(row=3, column=0, sticky="w", pady=2)
    render_ic_training_reference_panel()


def render_ic_training_reference_panel():
    """Show the Reference Data action set in the module's main pane."""
    show_right_panel("ic_training_helper")
    for child in right_frame.winfo_children():
        # Keep all persistent right-pane widgets alive.  In particular, destroying
        # the compact placeholder leaves ``compact_placeholder_label`` pointing at
        # an invalid Tcl window, so the next view change fails in grid_remove().
        if child not in (log_frame, shoot_api_frame, compact_placeholder_label):
            child.destroy()
    panel = ctk.CTkFrame(right_frame, fg_color="transparent")
    panel.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
    ctk.CTkLabel(panel, text="Reference Data", font=("Segoe UI", 16, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 12)
    )
    panel.grid_columnconfigure(0, weight=0)
    panel.grid_columnconfigure(1, weight=1)
    _render_ic_training_values(panel, start_row=1, column=1)
    actions = (
        ("Check Defaults", ic_check_defaults, "Default reference data checked and synced."),
        ("Create Dummy Customer", create_dummy_customer, "Dummy customer created."),
        ("Create Dummy Product", create_dummy_product, "Dummy product created."),
        ("Create Dummy Shipping Method", create_dummy_shipping_method, "Dummy shipping method created."),
        ("Do it all", ic_do_it_all, "All dummy reference data created."),
    )
    action_frame = ctk.CTkFrame(panel, fg_color="transparent")
    action_frame.grid(row=1, column=0, sticky="nw", padx=(0, 18), pady=2)
    for row, (label, action, success) in enumerate(actions):
        ctk.CTkButton(
            action_frame,
            text=label,
            width=225,
            height=36,
            corner_radius=7,
            command=lambda fn=action, message=success: run_ic_training_action(
                fn, message, render_ic_training_reference_panel
            ),
        ).grid(row=row, column=0, sticky="ew", pady=(0, 7))


def render_ic_training_sales_orders_panel():
    """Show sales-order preparation and creation actions."""
    show_right_panel("ic_training_helper")
    for child in right_frame.winfo_children():
        if child not in (log_frame, shoot_api_frame, compact_placeholder_label):
            child.destroy()
    panel = ctk.CTkFrame(right_frame, fg_color="transparent")
    panel.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
    panel.grid_columnconfigure(0, weight=0)
    panel.grid_columnconfigure(1, weight=1)
    ctk.CTkLabel(panel, text="Sales Orders", font=("Segoe UI", 16, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 12)
    )
    _render_ic_training_values(panel, start_row=1, column=1)
    actions = (
        ("Check Defaults", ic_check_defaults, "Default reference data checked and synced."),
        ("Create Ref Data", ic_do_it_all, "All dummy reference data created."),
        ("Create SO", create_sales_order, "Sales order created."),
    )
    action_frame = ctk.CTkFrame(panel, fg_color="transparent")
    action_frame.grid(row=1, column=0, sticky="nw", padx=(0, 18), pady=2)
    for row, (label, action, success) in enumerate(actions):
        ctk.CTkButton(
            action_frame,
            text=label,
            width=225,
            height=36,
            corner_radius=7,
            command=lambda fn=action, message=success: run_ic_training_action(
                fn, message, render_ic_training_sales_orders_panel
            ),
        ).grid(row=row, column=0, sticky="ew", pady=(0, 7))



def render_ic_training_inventory_panel():
    """Show inventory reference sync and random-allocation controls."""
    global ic_inventory_use_pricelist_var, ic_inventory_pricelist_var
    global ic_inventory_generic_value_var, ic_inventory_pricelist_options
    show_right_panel("ic_training_helper")
    for child in right_frame.winfo_children():
        if child not in (log_frame, shoot_api_frame, compact_placeholder_label):
            child.destroy()
    panel = ctk.CTkFrame(right_frame, fg_color="transparent")
    panel.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
    panel.grid_columnconfigure(1, weight=1)
    ctk.CTkLabel(panel, text="Inventory", font=("Segoe UI", 16, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 12)
    )
    account_name = (selected_account_var.get() or "").strip()
    counts = inventory_reference_values(get_data_db_path(account_name)) if account_name else {}
    snapshot = ctk.CTkFrame(panel)
    snapshot.grid(row=1, column=1, sticky="new", padx=(24, 0), pady=4)
    for row, (label, key) in enumerate((
        ("TOTAL SKUS", "skus"), ("TOTAL WAREHOUSES", "warehouses"),
        ("TOTAL WAREHOUSE LOCATIONS", "locations"), ("TOTAL PRICELISTS", "pricelists"),
        ("TOTAL PRICELIST ENTRIES", "pricelist_entries"),
    )):
        ctk.CTkLabel(snapshot, text=label, font=("Segoe UI", 11, "bold")).grid(
            row=row, column=0, sticky="w", padx=12, pady=6)
        ctk.CTkLabel(snapshot, text=str(counts.get(key, 0))).grid(
            row=row, column=1, sticky="e", padx=12, pady=6)

    actions = ctk.CTkFrame(panel, fg_color="transparent")
    actions.grid(row=1, column=0, sticky="nw", padx=(0, 18), pady=2)
    commands = (
        ("Check Defaults", render_ic_training_inventory_panel),
        ("Sync Products", run_product_catalogue_sync),
        ("Sync Warehouses", run_inventory_warehouse_sync),
        ("Sync Locations", run_location_catalogue_sync),
        ("Sync Pricelists", run_inventory_pricelist_sync),
        ("Sync All Ref Data", run_inventory_import_syncs),
    )
    for row, (label, command) in enumerate(commands):
        ctk.CTkButton(actions, text=label, width=225, height=36, command=command).grid(
            row=row, column=0, sticky="ew", pady=(0, 7)
        )

    quantity_var = tk.StringVar(master=root, value="10")
    ctk.CTkEntry(actions, textvariable=quantity_var, width=225, height=36).grid(
        row=12, column=0, sticky="ew", pady=(6, 7))
    def add_quick_stock():
        def action(account, db_path, **kwargs):
            return quick_stock(account, db_path, quantity=quantity_var.get(), **kwargs)
        run_ic_training_action(action, "Quick stock added.")
    ctk.CTkButton(actions, text="Quick Stock", width=225, height=36,
                  command=add_quick_stock).grid(row=13, column=0, sticky="ew", pady=(0, 12))

    ctk.CTkLabel(actions, text="Use pricelist").grid(row=6, column=0, sticky="w")
    ic_inventory_use_pricelist_var = tk.StringVar(master=root, value="N")
    radio_frame = ctk.CTkFrame(actions, fg_color="transparent")
    radio_frame.grid(row=7, column=0, sticky="w", pady=(0, 7))
    ctk.CTkRadioButton(radio_frame, text="Y", variable=ic_inventory_use_pricelist_var,
                       value="Y").pack(side="left", padx=(0, 10))
    ctk.CTkRadioButton(radio_frame, text="N", variable=ic_inventory_use_pricelist_var,
                       value="N").pack(side="left")
    try:
        rows = get_ref_price_lists(get_data_db_path(account_name)) if account_name else []
    except Exception:
        rows = []
    ic_inventory_pricelist_options = {f"{pid} - {name}": pid for pid, name in rows}
    labels = list(ic_inventory_pricelist_options) or ["No price lists synced"]
    ic_inventory_pricelist_var = tk.StringVar(master=root, value=labels[0])
    pricelist = ctk.CTkOptionMenu(actions, variable=ic_inventory_pricelist_var,
                                  values=labels, width=225)
    pricelist.grid(row=8, column=0, sticky="ew", pady=(0, 7))
    ic_inventory_generic_value_var = tk.StringVar(master=root, value="20")
    generic = ctk.CTkEntry(actions, textvariable=ic_inventory_generic_value_var, width=225)
    generic.grid(row=8, column=0, sticky="ew", pady=(0, 7))
    def toggle_cost(*_):
        if ic_inventory_use_pricelist_var.get() == "Y":
            generic.grid_remove()
            pricelist.grid()
        else:
            pricelist.grid_remove()
            generic.grid()
    ic_inventory_use_pricelist_var.trace_add("write", toggle_cost)
    toggle_cost()
    def allocate():
        def action(account, db_path, **kwargs):
            selected = ic_inventory_pricelist_var.get()
            price_id = ic_inventory_pricelist_options.get(selected) \
                if ic_inventory_use_pricelist_var.get() == "Y" else None
            if ic_inventory_use_pricelist_var.get() == "Y" and price_id is None:
                raise ValueError("Choose a synced pricelist first.")
            return allocate_random_inventory(
                account, db_path, price_list_id=price_id,
                generic_value=ic_inventory_generic_value_var.get(), **kwargs)
        run_ic_training_action(action, "Random inventory allocated.",
                               render_ic_training_inventory_panel, determinate=True)
    ctk.CTkButton(actions, text="Allocate Rando Inventory", width=225, height=36,
                  command=allocate).grid(row=9, column=0, sticky="ew", pady=(0, 7))


def _render_ic_training_values(panel, *, start_row, column):
    """Render the currently persisted IC helper values in the main pane."""
    account_name = (selected_account_var.get() or "").strip()
    values = (
        training_reference_values(account_name, get_data_db_path(account_name))
        if account_name else {}
    )
    fields = (
        ("CATEGORY ID", "categoryId"), ("BRAND ID", "brandId"),
        ("DEFAULT TAX RATE", "defaultTaxRate"), ("CHANNEL ID", "channelId"),
        ("ORDER STATUS ID", "statusId"), ("BASE CURRENCY", "baseCurrencyCode"),
        ("COUNTRY ISO", "countryIsoCode"), ("TAX CODE", "taxCode"),
        ("SHIPPING NOMINAL", "shippingNominalCode"),
        ("CONTACT ID", "contactId"), ("CONTACT NAME", ("firstName", "lastName")),
        ("PRODUCT ID", "productId"), ("PRODUCT SKU", "sku"),
        ("PRODUCT NAME", "productName"), ("SHIPPING METHOD ID", "shippingMethodId"),
    )
    data_frame = ctk.CTkFrame(panel)
    data_frame.grid(
        row=start_row, column=column,
        sticky="new", padx=(24, 0), pady=4,
    )
    for grid_column in (0, 1):
        data_frame.grid_columnconfigure(grid_column, weight=1, uniform="reference_data")
    ctk.CTkLabel(
        panel,
        text="REFERENCE SNAPSHOT",
        font=("Segoe UI", 12, "bold"),
        text_color=("#315A7D", "#72B7E8")
    ).grid(
        row=start_row - 1,
        column=column,
        sticky="w",
        padx=(24, 0),
        pady=(0, 4)
    )
    for index, (label, key) in enumerate(fields):
        row, grid_column = divmod(index, 2)
        cell = ctk.CTkFrame(data_frame, corner_radius=116, height=142)
        cell.grid(row=row, column=grid_column, sticky="nsew", padx=5, pady=4)
        if isinstance(key, tuple):
            value = " ".join(str(values.get(part, "")).strip() for part in key).strip()
        else:
            value = str(values.get(key, "")).strip()
        display_text = f"{label}: {value or '—'}"
        ctk.CTkLabel(cell, text=display_text, font=("Consolas", 10, "bold"), anchor="w", justify="left").pack(
            anchor="w", padx=6, pady=2
        )  

def render_shoot_apis_panel():
    global shoot_api_frame, api_method_var, api_url_var, api_request_text, api_response_text
    global api_status_var, send_api_button, retry_207_button, save_log_button, save_failed_log_button
    global response_meta_details_var, response_meta_details_label, response_meta_icon_label
    if shoot_api_frame is None:
        shoot_api_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
        shoot_api_frame.grid_columnconfigure(0, weight=1)
        shoot_api_frame.grid_rowconfigure(1, weight=1)

        if api_method_var is None:
            api_method_var = StringVar(master=root, value=API_METHODS[0])
        api_url_var = StringVar(master=root, value="")
        api_status_var = StringVar(value="")

        address_frame = ctk.CTkFrame(shoot_api_frame, fg_color="transparent")
        address_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        address_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(address_frame, text="URL").grid(row=0, column=0, sticky="w", padx=(0, 6))
        url_entry = ctk.CTkEntry(address_frame, textvariable=api_url_var)
        url_entry.grid(row=0, column=1, sticky="ew")

        status_label = ctk.CTkLabel(
            shoot_api_frame, textvariable=api_status_var, anchor="w"
        )
        status_label.grid(row=2, column=0, sticky="ew", padx=4, pady=(0, 4))

        save_log_button = ctk.CTkButton(
            shoot_api_frame,
            text="Save Success Log",
            command=save_run_log,
            width=140,
        )
        save_log_button.grid(row=2, column=0, sticky="e", padx=(4, 150), pady=(0, 4))
        save_log_button.grid_remove()

        save_failed_log_button = ctk.CTkButton(
            shoot_api_frame,
            text="Save Failed Log",
            command=save_failed_log,
            width=140,
        )
        save_failed_log_button.grid(row=2, column=0, sticky="e", padx=4, pady=(0, 4))
        save_failed_log_button.grid_remove()

        split_frame = ctk.CTkFrame(shoot_api_frame, fg_color="transparent")
        split_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        split_frame.grid_columnconfigure(0, weight=1)
        split_frame.grid_columnconfigure(1, weight=1)
        split_frame.grid_rowconfigure(1, weight=1)

        left_label = ctk.CTkLabel(split_frame, text="Request JSON")
        left_label.grid(row=0, column=0, sticky="w", padx=(0, 6))
        response_header_frame = ctk.CTkFrame(split_frame, fg_color="transparent")
        response_header_frame.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        response_header_frame.grid_columnconfigure(0, weight=1)
        right_label = ctk.CTkLabel(response_header_frame, text="Response")
        right_label.grid(row=0, column=0, sticky="w")
        response_meta_details_var = StringVar(value="")
        response_meta_details_label = ctk.CTkLabel(
            response_header_frame,
            textvariable=response_meta_details_var,
            anchor="e",
        )
        response_meta_details_label.grid(row=0, column=1, sticky="e", padx=(0, 6))
        response_meta_icon_label = ctk.CTkLabel(response_header_frame, text="")
        response_meta_icon_label.grid(row=0, column=2, sticky="e")

        left_text_frame = ctk.CTkFrame(split_frame)
        left_text_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        left_text_frame.grid_columnconfigure(0, weight=1)
        left_text_frame.grid_rowconfigure(0, weight=1)
        api_request_text = ctk.CTkTextbox(left_text_frame, wrap="none")
        api_request_text.grid(row=0, column=0, sticky="nsew")
        left_scroll = ctk.CTkScrollbar(left_text_frame, orientation=VERTICAL, command=api_request_text.yview)
        left_scroll.grid(row=0, column=1, sticky="ns")
        api_request_text.configure(yscrollcommand=left_scroll.set)

        right_text_frame = ctk.CTkFrame(split_frame)
        right_text_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        right_text_frame.grid_columnconfigure(0, weight=1)
        right_text_frame.grid_rowconfigure(0, weight=1)
        api_response_text = ctk.CTkTextbox(right_text_frame, wrap="none")
        api_response_text.grid(row=0, column=0, sticky="nsew")
        right_scroll = ctk.CTkScrollbar(right_text_frame, orientation=VERTICAL, command=api_response_text.yview)
        right_scroll.grid(row=0, column=1, sticky="ns")
        api_response_text.configure(yscrollcommand=right_scroll.set)
        api_response_text.configure(state="disabled")
        apply_api_text_fonts(get_settings())

        tk_response_text = getattr(api_response_text, "_textbox", api_response_text)
        response_menu = tk.Menu(tk_response_text, tearoff=0)

        def handle_add_response_mapping():
            if api_response_text is None:
                return
            tk_text = getattr(api_response_text, "_textbox", api_response_text)
            if not tk_text.tag_ranges("sel"):
                messagebox.showwarning("Select text", "Highlight a response value first.")
                return
            selection = tk_text.get("sel.first", "sel.last")
            open_response_mapping_dialog(selection)

        response_menu.add_command(label="Add to variable", command=handle_add_response_mapping)

        def open_response_menu(event):
            response_menu.tk_popup(event.x_root, event.y_root)

        tk_response_text.bind("<Button-3>", open_response_menu)
        tk_response_text.bind("<Button-2>", open_response_menu)

        send_api_button = None
        retry_207_button = None

    if send_api_button is None:
        for child in left_frame.winfo_children():
            if isinstance(child, ctk.CTkButton) and child.cget("text") in {"SEND", "SEND IT ONCE"}:
                send_api_button = child
                break

    refresh_shoot_api_defaults()

def render_inventory_import_view():
    global inventory_use_pricelist_var
    global inventory_pricelist_var
    global inventory_allow_zero_blanks_var
    global inventory_pricelist_dropdown
    global inventory_pricelist_controls_frame

    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Inventory Import", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync all", width=75,
           command=run_inventory_import_syncs).grid(row=1,rowspan=4, column=0, sticky="nsw", pady=2)
    ctk.CTkButton(left_frame, text="Sync Products", width=140,
           command=run_product_catalogue_sync).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Warehouses", width=140,
           command=run_inventory_warehouse_sync).grid(row=2, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Locations", width=140,
           command=run_location_catalogue_sync).grid(row=3, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Pricelists", width=140,
           command=run_inventory_pricelist_sync).grid(row=4, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_csv_template).grid(row=5, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_inventory_csv_validation).grid(row=6, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync to Brightpearl", width=220,
           command=run_inventory_sync).grid(row=7, column=0, sticky="w", pady=2)

    inventory_use_pricelist_var = StringVar(value="No")
    ctk.CTkLabel(left_frame, text="Use Pricelist").grid(row=8, column=0, sticky="w", pady=(8, 0))
    use_pl_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    use_pl_frame.grid(row=9, column=0, sticky="w", pady=(0, 2))
    ctk.CTkRadioButton(use_pl_frame, text="Yes", variable=inventory_use_pricelist_var, value="Yes").pack(side="left", padx=(0, 8))
    ctk.CTkRadioButton(use_pl_frame, text="No", variable=inventory_use_pricelist_var, value="No").pack(side="left")

    inventory_pricelist_var = StringVar(value="No price lists synced")
    inventory_pricelist_controls_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    inventory_pricelist_controls_frame.grid(row=10, column=0, sticky="ew", pady=(0, 4))
    inventory_pricelist_dropdown = ctk.CTkOptionMenu(
        inventory_pricelist_controls_frame,
        variable=inventory_pricelist_var,
        values=["No price lists synced"],
        width=220,
    )
    inventory_pricelist_dropdown.grid(row=0, column=0, sticky="w", pady=(0, 2))
    ctk.CTkButton(
        inventory_pricelist_controls_frame,
        text="Set Prices",
        width=220,
        command=run_set_inventory_prices,
    ).grid(row=1, column=0, sticky="w")

    inventory_allow_zero_blanks_var = StringVar(value="No")
    ctk.CTkLabel(left_frame, text="Allow Zero/Blanks").grid(row=11, column=0, sticky="w", pady=(8, 0))
    allow_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    allow_frame.grid(row=12, column=0, sticky="w", pady=(0, 2))
    ctk.CTkRadioButton(allow_frame, text="Yes", variable=inventory_allow_zero_blanks_var, value="Yes").pack(side="left", padx=(0, 8))
    ctk.CTkRadioButton(allow_frame, text="No", variable=inventory_allow_zero_blanks_var, value="No").pack(side="left")

    def toggle_pricelist_controls(*_args):
        if inventory_use_pricelist_var.get() == "Yes":
            inventory_pricelist_controls_frame.grid()
        else:
            inventory_pricelist_controls_frame.grid_remove()

    inventory_use_pricelist_var.trace_add("write", toggle_pricelist_controls)
    toggle_pricelist_controls()
    refresh_inventory_pricelist_options()

def render_additional_addresses_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Multiple Contact Addresses", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync Contact Catalogue", width=220,
           command=run_contact_catalogue_sync).grid(row=1, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_address_csv_template).grid(row=2, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Download contact IDs", width=220,
           command=download_contact_ids_csv).grid(row=3, column=0, sticky="w", pady=2)

    ctk.CTkLabel(left_frame, text="Use:").grid(row=4, column=0, sticky="w", pady=(10, 0))
    lookup_mode_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    lookup_mode_frame.grid(row=5, column=0, sticky="w", pady=(0, 2))
    ctk.CTkRadioButton(lookup_mode_frame, text="email", variable=address_contact_lookup_mode_var, value="email").pack(side="left", padx=(0, 8))
    ctk.CTkRadioButton(lookup_mode_frame, text="ID", variable=address_contact_lookup_mode_var, value="id").pack(side="left")

    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_address_csv_validation).grid(row=6, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync to Brightpearl", width=220,
           command=run_address_sync).grid(row=7, column=0, sticky="w", pady=2)

def render_experimental_sync_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="SYNC", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )

    ctk.CTkLabel(left_frame, text="Sync Contact Catalogue", font=("Segoe UI", 10, "bold")).grid(
        row=1, column=0, sticky="w", pady=(0, 2)
    )
    ctk.CTkLabel(left_frame, text="Append?", font=("Segoe UI", 10, "bold")).grid(
        row=2, column=0, sticky="w", pady=(0, 2)
    )
    toggle_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    toggle_frame.grid(row=3, column=0, sticky="w", pady=(0, 4))

    first_result_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    ctk.CTkLabel(first_result_frame, text="lastResult (firstResult)").grid(
        row=0, column=0, sticky="w", padx=(0, 8)
    )
    ctk.CTkEntry(first_result_frame, textvariable=contact_catalogue_first_result_var, width=110).grid(
        row=0, column=1, sticky="w"
    )

    def _toggle_first_result():
        append_enabled = ((contact_catalogue_append_var.get() or "No").strip().lower() == "yes")
        if append_enabled:
            first_result_frame.grid(row=4, column=0, sticky="w", pady=(0, 6))
        else:
            first_result_frame.grid_forget()

    ctk.CTkRadioButton(
        toggle_frame,
        text="No",
        variable=contact_catalogue_append_var,
        value="No",
        command=_toggle_first_result,
    ).grid(row=0, column=0, padx=(0, 10), sticky="w")
    ctk.CTkRadioButton(
        toggle_frame,
        text="Yes",
        variable=contact_catalogue_append_var,
        value="Yes",
        command=_toggle_first_result,
    ).grid(row=0, column=1, sticky="w")

    _toggle_first_result()

    ctk.CTkButton(left_frame, text="Sync Contact Catalogue", width=220,
           command=run_contact_catalogue_sync).grid(row=5, column=0, sticky="w", pady=2)

    ctk.CTkLabel(left_frame, text="Sync Order Catalogue", font=("Segoe UI", 10, "bold")).grid(
        row=6, column=0, sticky="w", pady=(10, 2)
    )
    ctk.CTkLabel(left_frame, text="Append?", font=("Segoe UI", 10, "bold")).grid(
        row=7, column=0, sticky="w", pady=(0, 2)
    )
    order_toggle_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    order_toggle_frame.grid(row=8, column=0, sticky="w", pady=(0, 4))

    order_first_result_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    ctk.CTkLabel(order_first_result_frame, text="lastResult (firstResult)").grid(
        row=0, column=0, sticky="w", padx=(0, 8)
    )
    ctk.CTkEntry(order_first_result_frame, textvariable=order_catalogue_first_result_var, width=110).grid(
        row=0, column=1, sticky="w"
    )

    def _toggle_order_first_result():
        append_enabled = ((order_catalogue_append_var.get() or "No").strip().lower() == "yes")
        if append_enabled:
            order_first_result_frame.grid(row=9, column=0, sticky="w", pady=(0, 6))
        else:
            order_first_result_frame.grid_forget()

    ctk.CTkRadioButton(
        order_toggle_frame,
        text="No",
        variable=order_catalogue_append_var,
        value="No",
        command=_toggle_order_first_result,
    ).grid(row=0, column=0, padx=(0, 10), sticky="w")
    ctk.CTkRadioButton(
        order_toggle_frame,
        text="Yes",
        variable=order_catalogue_append_var,
        value="Yes",
        command=_toggle_order_first_result,
    ).grid(row=0, column=1, sticky="w")

    _toggle_order_first_result()

    ctk.CTkButton(left_frame, text="Sync Order Catalogue", width=220,
           command=run_sales_order_catalogue_append_sync).grid(row=10, column=0, sticky="w", pady=2)

def render_warehouse_locations_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Warehouse Locations", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync all", width=75,
        command=run_warehouse_location_syncs).grid(row=1, rowspan=3, column=0, sticky="nsw", pady=2)
    ctk.CTkButton(left_frame, text="Sync warehouses", width=140,
        command=run_inventory_warehouse_sync).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Locations", width=140,
        command=run_location_catalogue_sync).grid(row=2, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Zones", width=140,
        command=run_zone_catalogue_sync).grid(row=3, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_location_csv_template).grid(row=4, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_warehouse_locations_validation).grid(row=5, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync Locations to Brightpearl", width=220,
           command=run_warehouse_locations_sync).grid(row=6, column=0, sticky="w", pady=2)
    ctk.CTkLabel(left_frame, text="Updating locations", font=("Segoe UI", 11, "bold")).grid(
        row=7, column=0, sticky="w", pady=(12, 6)
    )
    ctk.CTkButton(left_frame, text="Download all locations", width=220,
           command=download_all_locations_csv).grid(row=8, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and validate", width=220,
           command=run_warehouse_location_updates_validation).grid(row=9, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Update locations", width=220,
           command=run_warehouse_locations_update_sync).grid(row=10, column=0, sticky="w", pady=2)

def render_warehouse_zones_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Warehouse Zones", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync Zone Catalogue", width=220,
           command=run_zone_catalogue_sync).grid(row=1, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_zone_csv_template).grid(row=2, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_warehouse_zones_validation).grid(row=3, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync Zones to Brightpearl", width=220,
           command=run_warehouse_zones_sync).grid(row=4, column=0, sticky="w", pady=2)

def render_export_product_catalogue_view():
    global export_product_include_pricelists_var, export_product_include_suppliers_var
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Export > Product Catalogue", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    export_product_include_pricelists_var = StringVar(value="No")
    export_product_include_suppliers_var = StringVar(value="No")
    ctk.CTkButton(
        left_frame,
        text="Sync products",
        width=220,
        command=run_export_product_catalogue_sync,
    ).grid(row=1, column=0, sticky="w", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Export product",
        width=220,
        command=run_export_product_catalogue_csv,
    ).grid(row=2, column=0, sticky="w", pady=2)
    ctk.CTkLabel(left_frame, text="Include price lists", font=("Segoe UI", 11)).grid(
        row=3, column=0, sticky="w", pady=(8, 0)
    )
    ctk.CTkRadioButton(
        left_frame,
        text="No",
        variable=export_product_include_pricelists_var,
        value="No",
    ).grid(row=4, column=0, sticky="w", pady=2)
    ctk.CTkRadioButton(
        left_frame,
        text="Yes",
        variable=export_product_include_pricelists_var,
        value="Yes",
    ).grid(row=5, column=0, sticky="w", pady=2)
    ctk.CTkLabel(left_frame, text="Include suppliers", font=("Segoe UI", 11)).grid(
        row=6, column=0, sticky="w", pady=(8, 0)
    )
    ctk.CTkRadioButton(
        left_frame,
        text="No",
        variable=export_product_include_suppliers_var,
        value="No",
    ).grid(row=7, column=0, sticky="w", pady=2)
    ctk.CTkRadioButton(
        left_frame,
        text="Yes",
        variable=export_product_include_suppliers_var,
        value="Yes",
    ).grid(row=8, column=0, sticky="w", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Sync and Export",
        width=220,
        command=run_export_product_catalogue_sync_and_export,
    ).grid(row=9, column=0, sticky="w", pady=2)


def render_export_ip_stock_history_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Export > IP Stock History", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(
        left_frame,
        text="Upload audit trail CSV",
        width=220,
        command=run_ip_stock_history_import,
    ).grid(row=1, column=0, sticky="w", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Export CSVs by warehouse",
        width=220,
        command=run_ip_stock_history_export,
    ).grid(row=2, column=0, sticky="w", pady=2)


def render_product_import_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Product Import", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync Refs", width=108,
           command=run_product_reference_data_sync).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Products", width=108,
           command=run_product_catalogue_sync).grid(row=1, column=0, sticky="nsw", pady=2)        
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_product_import_csv_template).grid(row=2, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_product_import_csv_validation).grid(row=3, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Create missing refs and options", width=220,
           command=run_create_missing_product_references).grid(row=4, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Create products", width=220,
           command=run_create_products_from_import).grid(row=5, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Download productId, sku ref data", width=220,
           command=download_product_id_sku_reference).grid(row=6, column=0, sticky="w", pady=(12, 2))
    ctk.CTkButton(left_frame, text="Update Picker", width=220,
           command=open_product_update_picker).grid(row=7, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Validate the Updates", width=220,
           command=run_product_update_validation).grid(row=8, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Create missing update refs/options", width=220,
           command=run_create_missing_product_update_references).grid(row=9, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync updates", width=220,
           command=run_product_update_sync).grid(row=10, column=0, sticky="w", pady=2)

def render_contact_import_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Contact Import", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(
        left_frame,
        text="Sync all",
        width=75,
        command=run_contact_import_sync_all,
    ).grid(row=1, rowspan=2, column=0, sticky="nsw", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Sync Ref Data",
        width=140,
        command=run_reference_data_sync,
    ).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Sync Contacts",
        width=140,
        command=run_contact_catalogue_sync,
    ).grid(row=2, column=0, sticky="e", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Download CSV Template",
        width=220,
        command=download_contact_import_csv_template,
    ).grid(row=3, column=0, sticky="w", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Upload and Validate CSV",
        width=220,
        command=run_contact_import_csv_validation,
    ).grid(row=4, column=0, sticky="w", pady=2)
    ctk.CTkButton(
        left_frame,
        text="Create Contacts",
        width=220,
        command=run_contact_import_sync,
    ).grid(row=5, column=0, sticky="w", pady=2)

def render_custom_fields_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Custom Fields", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync all", width=75,
           command=run_custom_fields_sync_all).grid(row=1, rowspan=5, column=0, sticky="nsw", pady=2)
    ctk.CTkButton(left_frame, text="Sync Customer PCF", width=140,
           command=run_customer_pcf_sync).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Supplier PCF", width=140,
           command=run_supplier_pcf_sync).grid(row=2, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Product PCF", width=140,
           command=run_product_pcf_sync).grid(row=3, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Sales PCF", width=140,
           command=run_sales_pcf_sync).grid(row=4, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync SO Refs", width=220,
           command=run_sales_order_catalogue_sync).grid(row=6, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Purchase PCF", width=140,
           command=run_purchase_pcf_sync).grid(row=5, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync PO Refs", width=220,
           command=run_purchase_order_catalogue_sync).grid(row=7, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Download contact template", width=220,
           command=download_contact_pcf_template).grid(row=8, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Download product template", width=220,
           command=download_product_pcf_template).grid(row=9, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Download order template", width=220,
           command=download_order_pcf_template).grid(row=10, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_pcf_csv_validation).grid(row=11, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync PCF to Brightpearl", width=220,
           command=run_pcf_sync_to_brightpearl).grid(row=12, column=0, sticky="w", pady=2)

def run_all_syncs():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        try:
            progress_start_indeterminate()
            credentials = fetch_credentials(account_name)
            fetch_and_store_reference_tables(
                account_name,
                credentials.region,
                credentials.headers,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Sync all cancelled during reference data sync.")
                messagebox.showinfo("Cancelled", "Sync all cancelled.")
                return

            update_contact_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Sync all cancelled during contact catalogue sync.")
                messagebox.showinfo("Cancelled", "Sync all cancelled.")
                return

            update_product_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Sync all cancelled during product catalogue sync.")
                messagebox.showinfo("Cancelled", "Sync all cancelled.")
            else:
                append_log("✅ Sync all complete (reference data, contacts, products).")
                messagebox.showinfo("Done", "Sync all complete.")
        except Exception as exc:
            messagebox.showerror("Error", f"Sync all failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_inventory_import_syncs():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        try:
            progress_start_indeterminate()
            credentials = fetch_credentials(account_name)

            update_product_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Inventory import sync all cancelled during product sync.")
                messagebox.showinfo("Cancelled", "Inventory import sync all cancelled.")
                return

            fetch_and_store_reference_tables(
                account_name,
                credentials.region,
                credentials.headers,
                db_path,
                reference_keys=("warehouses",),
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Inventory import sync all cancelled during warehouse sync.")
                messagebox.showinfo("Cancelled", "Inventory import sync all cancelled.")
                return

            update_location_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Inventory import sync all cancelled during location sync.")
                messagebox.showinfo("Cancelled", "Inventory import sync all cancelled.")
                return

            sync_inventory_pricelists(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Inventory import sync all cancelled during pricelist sync.")
                messagebox.showinfo("Cancelled", "Inventory import sync all cancelled.")
            else:
                append_log("✅ Inventory import sync all complete.")
                messagebox.showinfo("Done", "Inventory import sync all complete.")
                root.after(0, refresh_inventory_pricelist_options)
        except Exception as exc:
            messagebox.showerror("Error", f"Inventory import sync all failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def run_all_gdpr_forget():
    run_forget_contacts()
    run_forget_contact_orders()
    
def run_inventory_pricelist_sync():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)
        try:
            progress_start_indeterminate()
            results = sync_inventory_pricelists(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Pricelist sync cancelled.")
                messagebox.showinfo("Cancelled", "Pricelist sync cancelled.")
            else:
                append_log(
                    f"✅ Pricelist sync complete. Lists: {results.get('price_lists', 0)}, values: {results.get('price_list_values', 0)}."
                )
                messagebox.showinfo("Done", "Pricelist references synced.")
                # Widget updates must run on Tk's main thread. The Inventory Import
                # controls may also have been destroyed after navigating to the IC
                # Training Helper, so the refresh routine verifies they still exist.
                root.after(0, refresh_inventory_pricelist_options)
        except Exception as exc:
            messagebox.showerror("Error", f"Pricelist sync failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def run_set_inventory_prices():
    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        messagebox.showerror("Error", "Choose an Active account first (top-right).")
        return

    if not inventory_pricelist_var:
        messagebox.showerror("Error", "Pricelist controls are unavailable.")
        return

    selected = (inventory_pricelist_var.get() or "").strip()
    if not selected or selected not in inventory_pricelist_options:
        messagebox.showerror("Error", "Choose a pricelist first.")
        return

    price_list_id = inventory_pricelist_options[selected]
    db_path = get_data_db_path(account_name)

    def task():
        cancel_token.clear()
        try:
            progress_start_indeterminate()
            result = set_inventory_costs_from_pricelist(
                account_name,
                db_path,
                price_list_id,
                log_callback=log_callback,
            )
            append_log(
                f"✅ Set prices complete. Updated {result['updated']} row(s). Missing: {result['missing_count']}, Zero: {result['zero_count']}."
            )
            messagebox.showinfo("Done", "Validated inventory cost prices updated from pricelist.")
        except Exception as exc:
            messagebox.showerror("Error", f"Set Prices failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()


def refresh_inventory_pricelist_options():
    global inventory_pricelist_options
    if inventory_pricelist_var is None or inventory_pricelist_dropdown is None:
        return
    try:
        if not inventory_pricelist_dropdown.winfo_exists():
            return
    except tk.TclError:
        # Navigating away destroys the option menu and its internal dropdown.
        # A completed background sync should still be considered successful.
        return

    account_name = (selected_account_var.get() or "").strip()
    if not account_name:
        return

    db_path = get_data_db_path(account_name)
    try:
        rows = get_ref_price_lists(db_path)
    except Exception:
        rows = []

    labels = []
    inventory_pricelist_options = {}
    for price_list_id, name in rows:
        label = f"{price_list_id} - {name}"
        labels.append(label)
        inventory_pricelist_options[label] = price_list_id

    try:
        inventory_pricelist_dropdown.configure(values=labels if labels else ["No price lists synced"])
        if labels:
            inventory_pricelist_var.set(labels[0])
        else:
            inventory_pricelist_var.set("No price lists synced")
    except tk.TclError:
        # The user can change panes between winfo_exists() and configure().
        return

def run_warehouse_location_syncs():
    def task():
        cancel_token.clear()
        account_name = (selected_account_var.get() or "").strip()
        if not account_name:
            messagebox.showerror("Error", "Choose an Active account first (top-right).")
            return

        db_path = get_data_db_path(account_name)

        try:
            progress_start_indeterminate()
            _sync_warehouses_reference_data(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Warehouse location sync all cancelled during warehouse sync.")
                messagebox.showinfo("Cancelled", "Warehouse location sync all cancelled.")
                return

            update_zone_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Warehouse location sync all cancelled during zone sync.")
                messagebox.showinfo("Cancelled", "Warehouse location sync all cancelled.")
                return

            update_location_catalogue(
                account_name,
                db_path,
                log_callback=log_callback,
                cancel_token=cancel_token,
            )
            if cancel_token.is_set():
                append_log("🛑 Warehouse location sync all cancelled during location sync.")
                messagebox.showinfo("Cancelled", "Warehouse location sync all cancelled.")
            else:
                append_log("✅ Warehouse location sync all complete.")
                messagebox.showinfo("Done", "Warehouse location sync all complete.")
        except Exception as exc:
            messagebox.showerror("Error", f"Warehouse location sync all failed:\n{exc}")
        finally:
            progress_stop_and_reset_to_determinate()

    threading.Thread(target=task, daemon=True).start()

def render_historic_sales_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Historic Orders", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync all", width=75,
           command=run_all_syncs).grid(row=1,rowspan=3, column=0, sticky="nsw", pady=2)
    ctk.CTkButton(left_frame, text="Sync Reference Data", width=140,
           command=run_reference_data_sync).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Contact Refs", width=140,
           command=run_contact_catalogue_sync).grid(row=2, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Product Refs", width=140,
           command=run_product_catalogue_sync).grid(row=3, column=0, sticky="e", pady=2)           
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_historic_orders_csv_template).grid(row=4, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_historic_orders_validation).grid(row=5, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync Historic Orders to Brightpearl", width=220,
           command=run_historic_orders_sync).grid(row=6, column=0, sticky="w", pady=2)
           
def render_open_sales_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Open Sales", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync all", width=75,
           command=run_all_syncs).grid(row=1,rowspan=3, column=0, sticky="nsw", pady=2)
    ctk.CTkButton(left_frame, text="Sync Reference Data", width=140,
           command=run_reference_data_sync).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Contact Refs", width=140,
           command=run_contact_catalogue_sync).grid(row=2, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Product Refs", width=140,
           command=run_product_catalogue_sync).grid(row=3, column=0, sticky="e", pady=2)             
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_sales_orders_csv_template).grid(row=4, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_sales_orders_validation).grid(row=5, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync Sales Orders to Brightpearl", width=220,
           command=run_sales_orders_sync).grid(row=6, column=0, sticky="w", pady=2)

def render_open_purchases_view():
    clear_left_panel()
    ctk.CTkLabel(left_frame, text="Open Purchases", font=("Segoe UI", 11, "bold")).grid(
        row=0, column=0, sticky="w", pady=(0, 6)
    )
    ctk.CTkButton(left_frame, text="Sync all", width=75,
           command=run_all_syncs).grid(row=1,rowspan=3, column=0, sticky="nsw", pady=2)
    ctk.CTkButton(left_frame, text="Sync Reference Data", width=140,
           command=run_reference_data_sync).grid(row=1, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Contact Refs", width=140,
           command=run_contact_catalogue_sync).grid(row=2, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Sync Product Refs", width=140,
           command=run_product_catalogue_sync).grid(row=3, column=0, sticky="e", pady=2)
    ctk.CTkButton(left_frame, text="Download CSV Template", width=220,
           command=download_open_purchases_csv_template).grid(row=4, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Upload and Validate CSV", width=220,
           command=run_open_purchases_validation).grid(row=5, column=0, sticky="w", pady=2)
    ctk.CTkButton(left_frame, text="Sync POs to Brightpearl", width=220,
           command=run_open_purchases_sync).grid(row=6, column=0, sticky="w", pady=2)

def render_forget_contact_view():
    clear_left_panel()

    global from_date_var, to_date_var
    from_date_var = StringVar(value="YYYY-MM-DD")
    to_date_var   = StringVar(value="YYYY-MM-DD")

    ctk.CTkLabel(
        left_frame,
        text="GDPR Forget Contacts",
        font=("Segoe UI", 11, "bold")
    ).grid(row=0, column=0, sticky="w", pady=(0,6))

    info_text = (
        "Step 1: Choose a date range.\n"
        "Step 2: Sync Contacts to Forget (downloads only contacts who last ordered in that range).\n"
        "Step 3: Run GDPR Forget (calls Brightpearl /forget on those IDs and marks them forgot=1).\n"
        "Step 4: Remove from Orders (calls order forget endpoint and marks forgot_order=1).\n"
    )
    ctk.CTkLabel(
        left_frame,
        text=info_text,
        justify="left",
        wraplength=260
    ).grid(row=1, column=0, sticky="w", pady=(0,8))

    ctk.CTkLabel(
        left_frame,
        text="Last Ordered On date range (YYYY-MM-DD):",
        font=("Segoe UI", 12, "bold")
    ).grid(row=2, column=0, sticky="w")

    date_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    date_frame.grid(row=3, column=0, sticky="w", pady=(4,12))

    ctk.CTkLabel(date_frame, text="From").grid(row=0, column=0, sticky="w", padx=(0,4))
    ctk.CTkEntry(
        date_frame,
        textvariable=from_date_var,
        width=100
    ).grid(row=0, column=1, sticky="w")

    ctk.CTkLabel(date_frame, text="to").grid(row=0, column=2, sticky="w", padx=(8,4))

    ctk.CTkEntry(
        date_frame,
        textvariable=to_date_var,
        width=100
    ).grid(row=0, column=3, sticky="w")

    ctk.CTkButton(
        left_frame,
        text="Sync Contacts to Forget",
        width=220,
        command=run_sync_forget_contacts_catalogue
    ).grid(row=4, column=0, sticky="w", pady=(0,6))

    ctk.CTkButton(
        left_frame,
        text="GDPR Forget Contact",
        width=220,
        command=run_forget_contacts
    ).grid(row=5, column=0, sticky="w", pady=(0,6))

    ctk.CTkButton(
        left_frame,
        text="GDPR Forget Order Contact",
        width=220,
        command=run_forget_contact_orders
    ).grid(row=6, column=0, sticky="w", pady=(0,6))

    ctk.CTkButton(
        left_frame,
        text="GDPR Forget All",
        width=220,
        command=run_all_gdpr_forget
    ).grid(row=7, column=0, sticky="w", pady=(0,6))


def render_warehouse_service_maintenance_view():
    global warehouse_service_var, warehouse_service_dropdown, warehouse_service_payload_mode_var
    clear_left_panel()

    ctk.CTkLabel(
        left_frame,
        text="Warehouse Service Maintenance",
        font=("Segoe UI", 11, "bold"),
    ).grid(row=0, column=0, sticky="w", pady=(0, 6))

    ctk.CTkButton(
        left_frame,
        text="Sync Warehouses",
        width=220,
        command=lambda: [run_inventory_warehouse_sync(), root.after(1200, refresh_warehouse_service_options)],
    ).grid(row=1, column=0, sticky="w", pady=2)

    warehouse_service_var = StringVar(value="Select warehouse")
    warehouse_service_dropdown = ctk.CTkOptionMenu(
        left_frame,
        variable=warehouse_service_var,
        values=["Select warehouse"],
        width=220,
    )
    warehouse_service_dropdown.grid(row=2, column=0, sticky="w", pady=2)

    ctk.CTkButton(
        left_frame,
        text="Upload CSV",
        width=220,
        command=run_warehouse_service_csv_import,
    ).grid(row=3, column=0, sticky="w", pady=2)

    ctk.CTkButton(
        left_frame,
        text="Process",
        width=220,
        command=run_warehouse_service_processing,
    ).grid(row=4, column=0, sticky="w", pady=2)

    warehouse_service_payload_mode_var = StringVar(value="Multi")
    ctk.CTkLabel(left_frame, text="POST Mode").grid(row=5, column=0, sticky="w", pady=(8, 0))
    payload_mode_frame = ctk.CTkFrame(left_frame, fg_color="transparent")
    payload_mode_frame.grid(row=6, column=0, sticky="w", pady=(0, 2))
    ctk.CTkRadioButton(
        payload_mode_frame,
        text="Multiple corrections per payload",
        variable=warehouse_service_payload_mode_var,
        value="Multi",
    ).pack(anchor="w")
    ctk.CTkRadioButton(
        payload_mode_frame,
        text="One correction per payload/API call",
        variable=warehouse_service_payload_mode_var,
        value="Single",
    ).pack(anchor="w")

    refresh_warehouse_service_options()
    
def render_left_panel():
    if current_tool_view == "inventory_import":
        render_inventory_import_view()
    elif current_tool_view == "additional_addresses":
        render_additional_addresses_view()
    elif current_tool_view == "warehouse_locations":
        render_warehouse_locations_view()
    elif current_tool_view == "warehouse_zones":
        render_warehouse_zones_view()
    elif current_tool_view == "product_import":
        render_product_import_view()
    elif current_tool_view == "export_product_catalogue":
        render_export_product_catalogue_view()
    elif current_tool_view == "export_ip_stock_history":
        render_export_ip_stock_history_view()
    elif current_tool_view == "contact_import":
        render_contact_import_view()
    elif current_tool_view == "custom_fields":
        render_custom_fields_view()
    elif current_tool_view == "open_sales":
        render_open_sales_view()
    elif current_tool_view == "open_purchases":
        render_open_purchases_view()
    elif current_tool_view == "historic_sales":
        render_historic_sales_view()
    elif current_tool_view == "forget_contact":
        render_forget_contact_view()    
    elif current_tool_view == "warehouse_service_maintenance":
        render_warehouse_service_maintenance_view()
    elif current_tool_view == "shoot_apis":
        render_shoot_apis_view()
    elif current_tool_view == "experimental_sync":
        render_experimental_sync_view()
    elif current_tool_view == "ic_training_helper":
        render_ic_training_helper_view()
    else:
        clear_left_panel()

    show_right_panel(current_tool_view)

def refresh_accounts_combo():
    accounts = list_saved_accounts()
    accounts_combo.configure(values=accounts)
    current = selected_account_var.get()
    if current in accounts:
        accounts_combo.set(current)
    elif accounts:
        selected_account_var.set(accounts[0]); accounts_combo.set(accounts[0])
    else:
        selected_account_var.set(""); accounts_combo.set("")
    refresh_shoot_api_defaults()

build_main_ui()
refresh_accounts_combo()
render_left_panel()
root.after_idle(show_main_window)
root.mainloop()
