import sqlite3
import sys

from brightpearl.common import connect_sqlite, ensure_processing_columns, ensure_account_binding, fetch_credentials_with_currency, log_sync, mark_rows_processed, unprocessed_where_clause
from sync_addresses import bp_post_json, make_headers

VALIDATED_TABLE = "validated_contacts"


def _bool_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
    return value if value != "" else None


def load_contacts_for_address(db_path):
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    ensure_processing_columns(conn, VALIDATED_TABLE)
    cursor.execute(
        f"""
        SELECT id, addressLine1, addressLine2, addressLine3, addressLine4,
               postalCode, countryIsoCode
        FROM {VALIDATED_TABLE}
        WHERE postalAddressId IS NULL
          AND {unprocessed_where_clause()}
        """
    )
    records = cursor.fetchall()
    conn.close()
    return records


def load_contacts_for_create(db_path):
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    ensure_processing_columns(conn, VALIDATED_TABLE)
    cursor.execute(
        f"""
        SELECT id, salutation, firstName, lastName, postalAddressId,
               email_PRI, email_SEC, email_TER, telephone_PRI, telephone_SEC,
               telephone_MOB, FAX, messagingVoips, website, isSupplier,
               isStaff, organisation_name, jobTitle, isReceiveEmailNewsletter,
               priceListId, taxCodeId, creditLimit, creditTermDays, currencyId,
               discountPercentage, taxNumber, staffOwnerContactId, accountReference
        FROM {VALIDATED_TABLE}
        WHERE postalAddressId IS NOT NULL
          AND contactId IS NULL
          AND {unprocessed_where_clause()}
        """
    )
    records = cursor.fetchall()
    conn.close()
    return records


def update_postal_address_id(db_path, record_id, postal_address_id):
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE {VALIDATED_TABLE} SET postalAddressId = ? WHERE id = ?",
        (postal_address_id, record_id),
    )
    conn.commit()
    conn.close()


def update_contact_id(db_path, record_id, contact_id):
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE {VALIDATED_TABLE} SET contactId = ? WHERE id = ?",
        (contact_id, record_id),
    )
    conn.commit()
    conn.close()



def mark_contact_processed(db_path, record_id, contact_id):
    conn = connect_sqlite(db_path)
    try:
        mark_rows_processed(conn, VALIDATED_TABLE, record_id, f"Created contact {contact_id}")
        conn.commit()
    finally:
        conn.close()

def build_contact_payload(row):
    (
        _record_id,
        salutation,
        first_name,
        last_name,
        postal_address_id,
        email_pri,
        email_sec,
        email_ter,
        tel_pri,
        tel_sec,
        tel_mob,
        fax,
        messaging_voips,
        website,
        is_supplier,
        is_staff,
        organisation_name,
        job_title,
        receive_newsletter,
        price_list_id,
        tax_code_id,
        credit_limit,
        credit_term_days,
        currency_id,
        discount_percentage,
        tax_number,
        staff_owner_contact_id,
        account_reference,
    ) = row

    payload = {
        "salutation": _clean(salutation),
        "firstName": _clean(first_name),
        "lastName": _clean(last_name),
        "postAddressIds": {
            "DEF": postal_address_id,
            "BIL": postal_address_id,
            "DEL": postal_address_id,
        },
    }

    organisation_name = _clean(organisation_name)
    if organisation_name:
        payload["organisation"] = {"name": organisation_name}

    if _clean(job_title):
        payload["jobTitle"] = _clean(job_title)

    communication = {}
    emails = {}
    if _clean(email_pri):
        emails["PRI"] = {"email": _clean(email_pri).lower()}
    if _clean(email_sec):
        emails["SEC"] = {"email": _clean(email_sec).lower()}
    if _clean(email_ter):
        emails["TER"] = {"email": _clean(email_ter).lower()}
    if emails:
        communication["emails"] = emails

    telephones = {}
    if _clean(tel_pri):
        telephones["PRI"] = _clean(tel_pri)
    if _clean(tel_sec):
        telephones["SEC"] = _clean(tel_sec)
    if _clean(tel_mob):
        telephones["MOB"] = _clean(tel_mob)
    if _clean(fax):
        telephones["FAX"] = _clean(fax)
    if telephones:
        communication["telephones"] = telephones

    if _clean(messaging_voips):
        communication["messagingVoips"] = {"SKP": _clean(messaging_voips)}

    if _clean(website):
        communication["websites"] = {"PRI": _clean(website)}

    if communication:
        payload["communication"] = communication

    relationship = {}
    supplier_flag = _bool_value(is_supplier)
    staff_flag = _bool_value(is_staff)
    if supplier_flag is not None:
        relationship["isSupplier"] = supplier_flag
    if staff_flag is not None:
        relationship["isStaff"] = staff_flag
    if relationship:
        payload["relationshipToAccount"] = relationship

    marketing = {}
    newsletter_flag = _bool_value(receive_newsletter)
    if newsletter_flag is not None:
        marketing["isReceiveEmailNewsletter"] = newsletter_flag
    if marketing:
        payload["marketingDetails"] = marketing

    financial = {}
    if price_list_id is not None:
        financial["priceListId"] = price_list_id
    if tax_code_id is not None:
        financial["taxCodeId"] = tax_code_id
    if credit_limit is not None:
        financial["creditLimit"] = credit_limit
    if credit_term_days is not None:
        financial["creditTermDays"] = credit_term_days
    if currency_id is not None:
        financial["currencyId"] = currency_id
    if discount_percentage is not None:
        financial["discountPercentage"] = discount_percentage
    if _clean(tax_number):
        financial["taxNumber"] = _clean(tax_number)
    if financial:
        payload["financialDetails"] = financial

    assignment = {}
    current_assignment = {}
    if staff_owner_contact_id is not None:
        current_assignment["staffOwnerContactId"] = staff_owner_contact_id
    if _clean(account_reference):
        current_assignment["accountReference"] = _clean(account_reference)
    if current_assignment:
        assignment["current"] = current_assignment
    if assignment:
        payload["assignment"] = assignment

    return payload


def update_progress(done, total, progress_callback=None):
    if progress_callback:
        progress_callback(done, max(total, 1))
    else:
        pct = int(100 * done / max(total, 1))
        print(f"PROGRESS:{pct}", flush=True)


def main(account_name, db_path, progress_callback=None, log_callback=None, cancel_token=None):
    ensure_account_binding(db_path, account_name)

    credentials, _base_currency = fetch_credentials_with_currency(account_name)
    headers = make_headers(credentials.app_ref, credentials.token)

    address_records = load_contacts_for_address(db_path)
    contact_records = load_contacts_for_create(db_path)
    total_steps = len(address_records) + len(contact_records)
    completed = 0

    log_sync(f"🔄 Posting {len(address_records)} postal addresses...", log_callback)

    for record in address_records:
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before next address POST.", log_callback)
            break

        rec_id, line1, line2, line3, line4, postal, country = record
        post_payload = {
            "addressLine1": line1,
            "addressLine2": line2,
            "addressLine3": line3,
            "addressLine4": line4,
            "postalCode": postal,
            "countryIsoCode": country,
        }

        post_url = (
            f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
            "contact-service/postal-address/"
        )
        resp_json = bp_post_json(post_url, headers, post_payload, log_callback, cancel_token)

        if resp_json is not None:
            postal_address_id = resp_json.get("response")
            if postal_address_id is not None:
                update_postal_address_id(db_path, rec_id, postal_address_id)
                log_sync(
                    f"✅ Address ID {postal_address_id} stored for contact row {rec_id}",
                    log_callback,
                )
            else:
                log_sync(
                    f"⚠️ POST OK but missing postalAddressId for contact row {rec_id}",
                    log_callback,
                )
        else:
            log_sync(f"❌ Failed to POST address for contact row {rec_id}", log_callback)

        completed += 1
        update_progress(completed, total_steps, progress_callback)

    log_sync("📦 Completed postal address posting.", log_callback)

    contact_records = load_contacts_for_create(db_path)
    log_sync(f"🔄 Creating {len(contact_records)} contacts...", log_callback)

    for record in contact_records:
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before next contact POST.", log_callback)
            break

        payload = build_contact_payload(record)
        post_url = (
            f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
            "contact-service/contact/"
        )
        resp_json = bp_post_json(post_url, headers, payload, log_callback, cancel_token)

        if resp_json is not None:
            contact_id = resp_json.get("response")
            if contact_id is not None:
                update_contact_id(db_path, record[0], contact_id)
                mark_contact_processed(db_path, record[0], contact_id)
                log_sync(
                    f"✅ Contact ID {contact_id} stored for contact row {record[0]}",
                    log_callback,
                )
            else:
                log_sync(
                    f"⚠️ POST OK but missing contactId for contact row {record[0]}",
                    log_callback,
                )
        else:
            log_sync(f"❌ Failed to POST contact row {record[0]}", log_callback)

        completed += 1
        update_progress(completed, total_steps, progress_callback)

    log_sync("✅ Contact creation complete.", log_callback)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python sync_contacts.py <account_name> <db_path>")
        sys.exit(1)

    acct = sys.argv[1]
    dbp = sys.argv[2]
    main(acct, dbp)
