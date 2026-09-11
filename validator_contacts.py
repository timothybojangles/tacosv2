import csv
from csv_safety import open_csv
import sqlite3
import os
import sys

from brightpearl.common import processing_column_definitions, connect_sqlite, ensure_account_binding, log_sync
from brightpearl.settings import get_settings

VALIDATED_TABLE = "validated_contacts"
REQUIRED_FIELDS = ("salutation", "lastName", "addressLine1", "postalCode")


def _normalize_header(value):
    if value is None:
        return ""
    return str(value).lstrip("\ufeff").strip()


def _safe_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _lookup_price_list(cur, value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    cur.execute(
        """
        SELECT priceListId
        FROM ref_price_lists
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
           OR LOWER(TRIM(code)) = LOWER(TRIM(?))
        LIMIT 1
        """,
        (text, text),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _lookup_tax_code(cur, value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    cur.execute(
        """
        SELECT taxCodeId
        FROM ref_taxcode
        WHERE LOWER(TRIM(code)) = LOWER(TRIM(?))
        LIMIT 1
        """,
        (text,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _lookup_currency(cur, value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    cur.execute(
        """
        SELECT currencyId
        FROM ref_currencies
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
           OR LOWER(TRIM(isoCode)) = LOWER(TRIM(?))
        LIMIT 1
        """,
        (text, text),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _lookup_staff_owner(cur, value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    name_parts = [part.strip() for part in text.replace(",", " ").split() if part.strip()]
    first_name = name_parts[0] if name_parts else None
    last_name = name_parts[1] if len(name_parts) > 1 else None

    cur.execute(
        """
        SELECT contactId
        FROM contact_catalogue
        WHERE isStaff = 1
          AND (
            LOWER(TRIM(firstName)) = LOWER(TRIM(?))
            OR LOWER(TRIM(lastName)) = LOWER(TRIM(?))
            OR (
                LOWER(TRIM(firstName)) = LOWER(TRIM(?))
                AND LOWER(TRIM(lastName)) = LOWER(TRIM(?))
            )
          )
        ORDER BY contactId
        LIMIT 1
        """,
        (text, text, first_name, last_name),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _has_duplicate_email(cur, emails):
    for email in emails:
        if not email:
            continue
        cur.execute(
            """
            SELECT contactId
            FROM contact_catalogue
            WHERE LOWER(TRIM(primaryEmail)) = LOWER(TRIM(?))
               OR LOWER(TRIM(secondaryEmail)) = LOWER(TRIM(?))
            LIMIT 1
            """,
            (email, email),
        )
        row = cur.fetchone()
        if row:
            return True
    return False


def _table_exists(cur, table_name):
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cur.fetchone() is not None


def validate_contacts(csv_path, db_path, account_name, region=None, log_callback=None, cancel_token=None):
    ensure_account_binding(db_path, account_name)

    if not os.path.exists(db_path):
        if log_callback:
            log_callback(f"❌ Database not found: {db_path}")
        return 0

    conn = connect_sqlite(db_path)
    cur = conn.cursor()

    cur.execute(f"DROP TABLE IF EXISTS {VALIDATED_TABLE}")
    cur.execute(
        f"""
        CREATE TABLE {VALIDATED_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            salutation TEXT,
            firstName TEXT,
            lastName TEXT,
            addressLine1 TEXT,
            addressLine2 TEXT,
            addressLine3 TEXT,
            addressLine4 TEXT,
            postalCode TEXT,
            countryIsoCode TEXT,
            email_PRI TEXT,
            email_SEC TEXT,
            email_TER TEXT,
            telephone_PRI TEXT,
            telephone_SEC TEXT,
            telephone_MOB TEXT,
            FAX TEXT,
            messagingVoips TEXT,
            website TEXT,
            isSupplier BOOLEAN,
            isStaff BOOLEAN,
            organisation_name TEXT,
            jobTitle TEXT,
            isReceiveEmailNewsletter BOOLEAN,
            priceList TEXT,
            taxcode TEXT,
            creditLimit REAL,
            creditTermDays INTEGER,
            currencyIso TEXT,
            discountPercentage REAL,
            taxNumber TEXT,
            staffOwnerName TEXT,
            accountReference TEXT,
            priceListId INTEGER,
            taxCodeId INTEGER,
            currencyId INTEGER,
            staffOwnerContactId INTEGER,
            postalAddressId INTEGER,
            contactId INTEGER{processing_column_definitions()}
        )
        """
    )

    unmatched = []
    duplicate_emails = []
    inserted_count = 0

    with open_csv(csv_path) as csvfile:
        reader = csv.DictReader(csvfile)
        raw_fieldnames = reader.fieldnames[:] if reader.fieldnames else []
        fieldnames = [_normalize_header(name) for name in raw_fieldnames if name is not None]
        has_email_columns = any(
            name in (fieldnames or [])
            for name in ("email_PRI", "email_SEC", "email_TER")
        )
        missing_headers = [name for name in REQUIRED_FIELDS if name not in (fieldnames or [])]
        if missing_headers:
            log_sync(
                f"❌ CSV is missing required headers: {', '.join(missing_headers)}",
                log_callback,
            )
            conn.close()
            return 0

        if has_email_columns and not _table_exists(cur, "contact_catalogue"):
            log_sync("❌ contact_catalogue is missing. Sync Contact Refs first.", log_callback)
            conn.close()
            return 0

        if "staffOwnerName" in (fieldnames or []) and not _table_exists(cur, "contact_catalogue"):
            log_sync("❌ contact_catalogue is missing. Sync Contact Refs first.", log_callback)
            conn.close()
            return 0

        if "priceList" in (fieldnames or []) and not _table_exists(cur, "ref_price_lists"):
            log_sync("❌ ref_price_lists is missing. Sync Contact Reference Data first.", log_callback)
            conn.close()
            return 0

        if "taxcode" in (fieldnames or []) and not _table_exists(cur, "ref_taxcode"):
            log_sync("❌ ref_taxcode is missing. Sync Contact Reference Data first.", log_callback)
            conn.close()
            return 0

        if "currencyIso" in (fieldnames or []) and not _table_exists(cur, "ref_currencies"):
            log_sync("❌ ref_currencies is missing. Sync Contact Reference Data first.", log_callback)
            conn.close()
            return 0

        for raw_row in reader:
            if cancel_token and cancel_token.is_set():
                break

            row = {
                _normalize_header(key): value
                for key, value in (raw_row or {}).items()
                if key is not None
            }

            salutation = _clean(row.get("salutation"))
            last_name = _clean(row.get("lastName"))
            address_line1 = _clean(row.get("addressLine1"))
            postal_code = _clean(row.get("postalCode"))

            if not all([salutation, last_name, address_line1, postal_code]):
                row_with_reason = dict(row)
                row_with_reason["failureReason"] = (
                    "Missing required fields (salutation, lastName, addressLine1, postalCode)"
                )
                unmatched.append(row_with_reason)
                continue

            email_pri = _clean(row.get("email_PRI"))
            if email_pri is not None:
                email_pri = email_pri.lower()
            email_sec = _clean(row.get("email_SEC"))
            if email_sec is not None:
                email_sec = email_sec.lower()
            email_ter = _clean(row.get("email_TER"))
            if email_ter is not None:
                email_ter = email_ter.lower()

            if has_email_columns:
                if _has_duplicate_email(cur, [email_pri, email_sec, email_ter]):
                    row_with_reason = dict(row)
                    row_with_reason["failureReason"] = "Duplicate email address found"
                    duplicate_emails.append(row_with_reason)
                    continue

            price_list_id = None
            price_list_value = _clean(row.get("priceList"))
            if price_list_value is not None:
                price_list_id = _lookup_price_list(cur, price_list_value)
                if price_list_id is None:
                    row_with_reason = dict(row)
                    row_with_reason["failureReason"] = f"Price list not found: {price_list_value}"
                    unmatched.append(row_with_reason)
                    continue

            tax_code_id = None
            tax_code_value = _clean(row.get("taxcode"))
            if tax_code_value is not None:
                tax_code_id = _lookup_tax_code(cur, tax_code_value)
                if tax_code_id is None:
                    row_with_reason = dict(row)
                    row_with_reason["failureReason"] = f"Tax code not found: {tax_code_value}"
                    unmatched.append(row_with_reason)
                    continue

            currency_id = None
            currency_value = _clean(row.get("currencyIso"))
            if currency_value is not None:
                currency_id = _lookup_currency(cur, currency_value)
                if currency_id is None:
                    row_with_reason = dict(row)
                    row_with_reason["failureReason"] = f"Currency not found: {currency_value}"
                    unmatched.append(row_with_reason)
                    continue

            staff_owner_id = None
            staff_owner_value = _clean(row.get("staffOwnerName"))
            if staff_owner_value is not None:
                staff_owner_id = _lookup_staff_owner(cur, staff_owner_value)
                if staff_owner_id is None:
                    row_with_reason = dict(row)
                    row_with_reason["failureReason"] = (
                        f"Staff owner not found: {staff_owner_value}"
                    )
                    unmatched.append(row_with_reason)
                    continue

            cur.execute(
                f"""
                INSERT INTO {VALIDATED_TABLE} (
                    salutation, firstName, lastName, addressLine1, addressLine2,
                    addressLine3, addressLine4, postalCode, countryIsoCode,
                    email_PRI, email_SEC, email_TER, telephone_PRI, telephone_SEC,
                    telephone_MOB, FAX, messagingVoips, website, isSupplier,
                    isStaff, organisation_name, jobTitle, isReceiveEmailNewsletter,
                    priceList, taxcode, creditLimit, creditTermDays, currencyIso,
                    discountPercentage, taxNumber, staffOwnerName, accountReference,
                    priceListId, taxCodeId, currencyId, staffOwnerContactId
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    salutation,
                    _clean(row.get("firstName")),
                    last_name,
                    address_line1,
                    _clean(row.get("addressLine2")),
                    _clean(row.get("addressLine3")),
                    _clean(row.get("addressLine4")),
                    postal_code,
                    _clean(row.get("countryIsoCode")),
                    email_pri,
                    email_sec,
                    email_ter,
                    _clean(row.get("telephone_PRI")),
                    _clean(row.get("telephone_SEC")),
                    _clean(row.get("telephone_MOB")),
                    _clean(row.get("FAX")),
                    _clean(row.get("messagingVoips")),
                    _clean(row.get("website")),
                    _parse_bool(row.get("isSupplier")),
                    _parse_bool(row.get("isStaff")),
                    _clean(row.get("organisation_name")),
                    _clean(row.get("jobTitle")),
                    _parse_bool(row.get("isReceiveEmailNewsletter")),
                    price_list_value,
                    tax_code_value,
                    _safe_float(_clean(row.get("creditLimit"))),
                    _safe_int(_clean(row.get("creditTermDays"))),
                    currency_value,
                    _safe_float(_clean(row.get("discountPercentage"))),
                    _clean(row.get("taxNumber")),
                    staff_owner_value,
                    _clean(row.get("accountReference")),
                    price_list_id,
                    tax_code_id,
                    currency_id,
                    staff_owner_id,
                ),
            )
            inserted_count += 1

    conn.commit()
    conn.close()

    log_sync("✅ Contacts validated and enriched.", log_callback)
    log_sync(
        f"✔️ Validated contacts inserted into {VALIDATED_TABLE}: {inserted_count}",
        log_callback,
    )

    unmatched_dir = get_settings().unmatched_output_dir
    if unmatched_dir:
        os.makedirs(unmatched_dir, exist_ok=True)

        if unmatched:
            failure_fieldnames = list(fieldnames)
            if "failureReason" not in failure_fieldnames:
                failure_fieldnames.append("failureReason")
            unmatched_file = os.path.join(
                unmatched_dir, f"{account_name}_unmatched_contacts.csv"
            )
            with open(unmatched_file, "w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=failure_fieldnames)
                writer.writeheader()
                writer.writerows(unmatched)
            log_sync(
                f"⚠️ {len(unmatched)} unmatched contacts written to {unmatched_file}",
                log_callback,
            )
            log_sync("⚠️ Validation failed for some rows (see unmatched report).", log_callback)

        if duplicate_emails:
            failure_fieldnames = list(fieldnames)
            if "failureReason" not in failure_fieldnames:
                failure_fieldnames.append("failureReason")
            dup_file = os.path.join(
                unmatched_dir,
                f"{account_name}_duplicate_contact_emails.csv",
            )
            with open(dup_file, "w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=failure_fieldnames)
                writer.writeheader()
                writer.writerows(duplicate_emails)
            log_sync(
                f"⚠️ {len(duplicate_emails)} contacts with duplicate emails written to {dup_file}",
                log_callback,
            )
            log_sync("⚠️ Duplicate email addresses found (see duplicate email report).", log_callback)

    return inserted_count


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python validator_contacts.py <account_name> <db_path>")
        sys.exit(1)

    account_name = sys.argv[1]
    db_path = sys.argv[2]

    file_path = input("CSV path: ").strip()
    if file_path:
        validate_contacts(file_path, db_path, account_name)
