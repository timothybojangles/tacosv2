import csv
from csv_safety import open_csv
import sqlite3
import os
import sys
from tkinter import Tk, filedialog

from brightpearl.common import processing_column_definitions, connect_sqlite, ensure_account_binding, log as bp_log
from brightpearl.settings import get_settings

VALIDATED_TABLE = "validated_addresses"

def select_csv_file():
    root = Tk()
    root.withdraw()
    file_path = filedialog.askopenfilename(
        filetypes=[("Spreadsheet files", "*.csv *.xlsx"), ("CSV files", "*.csv"), ("Excel workbooks", "*.xlsx")],
        title="Select CSV file for address validation"
    )
    return file_path

def log(msg):
    bp_log(f"[validator_addresses] {msg}", log_file=None)
    
def validate_addresses(
    csv_path,
    db_path,
    account_name,
    region=None,
    log_callback=None,
    cancel_token=None,
    contact_lookup_mode="email",
):
    if not os.path.exists(db_path):
        if log_callback:
            log_callback(f"❌ Database not found: {db_path}")
        return 0  # nothing inserted

    conn = connect_sqlite(db_path)
    cur = conn.cursor()

    # Prepare validated_addresses table fresh each run
    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            primaryEmail TEXT,
            addressLine1 TEXT,
            addressLine2 TEXT,
            addressLine3 TEXT,
            addressLine4 TEXT,
            postalCode TEXT,
            countryIsoCode TEXT,
            contactId INTEGER,
            postalAddressId INTEGER{processing_column_definitions()}
        )
    """)

    unmatched = []
    inserted_count = 0  # <-- we'll track how many rows we actually insert

    # IMPORTANT: capture the CSV header fields so we can reuse them in the unmatched file
    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = reader.fieldnames[:] if reader.fieldnames else []

        for row in reader:
            if cancel_token and cancel_token.is_set():
                # honour cancel and stop processing more rows
                break

            email = row.get("emailAddress", "").strip().lower()

            if contact_lookup_mode == "id":
                contact_id_raw = row.get("contactId", "").strip()
                if not contact_id_raw:
                    unmatched.append(row)
                    continue
                try:
                    contact_id = int(contact_id_raw)
                except ValueError:
                    unmatched.append(row)
                    continue
            else:
                if not email:
                    unmatched.append(row)
                    continue

                # Use a case-insensitive/trimmed match so we don't accidentally grab
                # the first contact in the table when the casing/spacing differs.
                cur.execute("""
                    SELECT contactId
                    FROM contact_catalogue
                    WHERE LOWER(TRIM(primaryEmail)) = LOWER(TRIM(?))
                    ORDER BY contactId
                    LIMIT 1
                """, (email,))

                result = cur.fetchone()

                if not result:
                    unmatched.append(row)
                    continue

                contact_id = result[0]

            cur.execute(f"""
                INSERT INTO {VALIDATED_TABLE} (
                    primaryEmail, addressLine1, addressLine2, addressLine3,
                    addressLine4, postalCode, countryIsoCode, contactId
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                email,
                row.get("addressLine1", "").strip(),
                row.get("addressLine2", "").strip(),
                row.get("addressLine3", "").strip(),
                row.get("addressLine4", "").strip(),
                row.get("postalCode", "").strip(),
                row.get("countryIsoCode", "").strip(),
                contact_id
            ))

            inserted_count += 1  # <-- bump count when we add a validated row

    conn.commit()
    conn.close()

    # logging to the right-hand pane
    if log_callback:
        mode_display = "contactId (CSV)" if contact_lookup_mode == "id" else "primaryEmail"
        log_callback(f"✅ Addresses validated and enriched using {mode_display}.")
        log_callback(f"✔️ Validated items inserted into {VALIDATED_TABLE}: {inserted_count}")

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)

    # write unmatched CSV if there are any
    if unmatched:
        unmatched_file = os.path.join(unmatched_dir, f"{account_name}_unmatched_addresses.csv")
        with open(unmatched_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(unmatched)

        if log_callback:
            log_callback(f"⚠️ {len(unmatched)} unmatched addresses written to {unmatched_file}")
    else:
        if log_callback:
            log_callback("✅ All addresses matched successfully.")

    # <-- THIS IS THE KEY BIT
    return inserted_count

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python validator_addresses.py <account_name> <db_path>")
        sys.exit(1)

    account_name = sys.argv[1]
    db_path = sys.argv[2]

    file_path = select_csv_file()
    if file_path:
        validate_addresses(file_path, db_path, account_name)
    else:
        log("No file selected.")
