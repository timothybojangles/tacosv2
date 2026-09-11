import sqlite3
import requests
import time
import sys
import csv
import os
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from brightpearl.common import (
    connect_sqlite,
    ensure_processing_columns,
    SYNC_DEBUG_LOG,
    ensure_account_binding,
    fetch_credentials_with_currency,
    log_sync,
    log_payload_exchange,
    mark_rows_processed,
    unprocessed_where_clause,
)
from brightpearl.performance import estimate_record_count, record_api_update
from brightpearl.settings import get_settings, get_upload_retry_settings
from brightpearl.throttle import parse_int_header, sleep_with_log, throttle_decision

urllib3.disable_warnings(InsecureRequestWarning)


def make_headers(app_ref, token):
    return {
        "brightpearl-app-ref": app_ref,
        "brightpearl-account-token": token,
        "Content-Type": "application/json"
    }

def load_validated_addresses(db_path):
    """
    Get addresses we still need to POST to Brightpearl.
    Expect validated_addresses columns:
      id, contactId, addressLine1, addressLine2, addressLine3,
      addressLine4, postalCode, countryIsoCode, postalAddressId
    """
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    ensure_processing_columns(conn, "validated_addresses")
    cursor.execute(f"""
        SELECT id,
               contactId,
               addressLine1,
               addressLine2,
               addressLine3,
               addressLine4,
               postalCode,
               countryIsoCode
        FROM validated_addresses
        WHERE postalAddressId IS NULL
          AND {unprocessed_where_clause()}
    """)
    records = cursor.fetchall()
    conn.close()
    return records

def update_postal_address_id(db_path, record_id, postalAddressId):
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE validated_addresses SET postalAddressId = ? WHERE id = ?",
        (postalAddressId, record_id)
    )
    conn.commit()
    conn.close()

def fetch_records_with_postal_ids(db_path):
    """
    Get (contactId, postalAddressId) for all rows that already have a created postalAddressId.
    We'll assign these to the contacts in Brightpearl via PUT.
    """
    conn = connect_sqlite(db_path)
    cursor = conn.cursor()
    ensure_processing_columns(conn, "validated_addresses")
    cursor.execute(f"""
        SELECT id, contactId, postalAddressId
        FROM validated_addresses
        WHERE postalAddressId IS NOT NULL
          AND {unprocessed_where_clause()}
    """)
    records = cursor.fetchall()
    conn.close()
    return records

def update_progress(current, total):
    progress = current / max(total, 1)
    bar_length = 40
    block = int(round(bar_length * progress))
    text = f"\rProgress: [{'#' * block + '-' * (bar_length - block)}] {int(progress * 100)}%"
    sys.stdout.write(text)
    sys.stdout.flush()
    print(f"\nPROGRESS:{int(progress * 100)}", flush=True)

# ---- Brightpearl POST helper ----
def bp_post_json(url, headers, payload, log_callback=None, cancel_token=None):
    """
    POST a single JSON payload to Brightpearl, with retry, throttling, and cancel support.
    Returns parsed JSON (dict) on success, or None on total failure.
    """
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before POST.", log_callback)
            return None
        try:
            resp = requests.post(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("POST", url, payload, resp, log_callback)
            log_sync(f"📮 POST {url} → {resp.status_code}", log_callback)

            if resp.status_code == 200:
                record_api_update(estimate_record_count(payload))
                # throttle headers
                requests_remaining = parse_int_header(resp.headers, "brightpearl-requests-remaining", 2)
                throttle_ms = parse_int_header(resp.headers, "brightpearl-next-throttle-period", 0)
                log_sync(
                    f"🔎 Rate-limit headers: requests_remaining={requests_remaining}, "
                    f"next_throttle_ms={throttle_ms}",
                    log_callback
                )

                data = resp.json()

                # polite / throttle sleep
                sleep_ms, reason = throttle_decision(
                    requests_remaining,
                    throttle_ms,
                    threshold=get_settings().throttle_threshold,
                )
                if sleep_ms > 0:
                    sleep_with_log(
                        sleep_ms,
                        log_callback,
                        cancel_token=cancel_token,
                        reason=reason,
                        log_file=SYNC_DEBUG_LOG,
                    )
                return data
            else:
                log_sync(f"❌ POST failed (attempt {attempt}) {resp.status_code}", log_callback)
                try:
                    log_sync(f"📥 Response body:\n{resp.text}", log_callback)
                except Exception as e:
                    log_sync(f"⚠️ Could not read response body: {e}", log_callback)

        except requests.RequestException as e:
            log_sync(f"❌ POST exception (attempt {attempt}): {e}", log_callback)

        # backoff before retry
        sleep_with_log(
            default_sleep_ms * attempt,
            log_callback,
            cancel_token=cancel_token,
            reason="retry backoff",
            log_file=SYNC_DEBUG_LOG,
        )

    return None  # total failure

# ---- Brightpearl PUT helper ----
def bp_put_json(url, headers, payload, log_callback=None, cancel_token=None):
    """
    PUT a JSON payload to Brightpearl, with retry, throttling, and cancel support.
    Returns True on success, False on total failure.
    """
    max_retries, default_sleep_ms = get_upload_retry_settings()
    for attempt in range(1, max_retries + 1):
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before PUT.", log_callback)
            return False
        try:
            resp = requests.put(url, json=payload, headers=headers, verify=False, timeout=60)
            log_payload_exchange("PUT", url, payload, resp, log_callback)
#            log_sync(f"📮 PUT {url} → {resp.status_code}", log_callback)

            if resp.status_code == 200:
                record_api_update(estimate_record_count(payload))
                requests_remaining = parse_int_header(resp.headers, "brightpearl-requests-remaining", 2)
                throttle_ms = parse_int_header(resp.headers, "brightpearl-next-throttle-period", 0)
                log_sync(
                    f"🔎 Rate-limit headers: requests_remaining={requests_remaining}, "
                    f"next_throttle_ms={throttle_ms}",
                    log_callback
                )

                sleep_ms, reason = throttle_decision(
                    requests_remaining,
                    throttle_ms,
                    threshold=get_settings().throttle_threshold,
                )
                if sleep_ms > 0:
                    sleep_with_log(
                        sleep_ms,
                        log_callback,
                        cancel_token=cancel_token,
                        reason=reason,
                        log_file=SYNC_DEBUG_LOG,
                    )
                return True
            else:
                log_sync(f"❌ PUT failed (attempt {attempt}) {resp.status_code}", log_callback)
                try:
                    log_sync(f"📥 Response body:\n{resp.text}", log_callback)
                except Exception as e:
                    log_sync(f"⚠️ Could not read response body: {e}", log_callback)

        except requests.RequestException as e:
            log_sync(f"❌ PUT exception (attempt {attempt}): {e}", log_callback)

        sleep_with_log(
            default_sleep_ms * attempt,
            log_callback,
            cancel_token=cancel_token,
            reason="retry backoff",
            log_file=SYNC_DEBUG_LOG,
        )

    return False  # total failure

def main(account_name, db_path, progress_callback=None, log_callback=None, cancel_token=None):
    # Make sure we're writing to the correct account DB
    ensure_account_binding(db_path, account_name)

    # Credentials
    credentials, _base_currency = fetch_credentials_with_currency(account_name)
    app_ref, token, region = credentials.app_ref, credentials.token, credentials.region
    headers = make_headers(app_ref, token)

    # 1. POST new postal addresses and capture postalAddressId
    address_records = load_validated_addresses(db_path)
    log_sync(f"🔄 Posting {len(address_records)} addresses to Brightpearl...", log_callback)

    total_addr = len(address_records)
    for i, row in enumerate(address_records, 1):
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before next address POST.", log_callback)
            break

        rec_id, contactId, line1, line2, line3, line4, postal, country = row

        post_payload = {
            "addressLine1": line1,
            "addressLine2": line2,
            "addressLine3": line3,
            "addressLine4": line4,
            "postalCode":  postal,
            "countryIsoCode": country
        }

        post_url = f"https://{region}.brightpearlconnect.com/public-api/{account_name}/contact-service/postal-address/"
        resp_json = bp_post_json(post_url, headers, post_payload, log_callback, cancel_token)

        if resp_json is not None:
            # Brightpearl tends to return {"response": <postalAddressId>}
            postalAddressId = resp_json.get("response")
            if postalAddressId is not None:
                update_postal_address_id(db_path, rec_id, postalAddressId)
                log_sync(f"✅ Address ID {postalAddressId} stored for contact {contactId}", log_callback)
            else:
                log_sync(f"⚠️ POST OK but missing postalAddressId for contact {contactId}", log_callback)
        else:
            log_sync(f"❌ Failed to POST address for contactId {contactId}", log_callback)

        if progress_callback:
            progress_callback(i, max(total_addr, 1))
        else:
            update_progress(i, max(total_addr, 1))

    log_sync("📦 Completed address posting.", log_callback)

    # 2. PUT each postalAddressId to the contact record (type 'DEL')
    contact_updates = fetch_records_with_postal_ids(db_path)
    log_sync(f"🔄 Linking {len(contact_updates)} postal addresses to contacts...", log_callback)

    total_links = len(contact_updates)
    for j, (record_id, contactId, postalAddressId) in enumerate(contact_updates, 1):
        if cancel_token and cancel_token.is_set():
            log_sync("🛑 Cancel before next contact PUT.", log_callback)
            break

        put_url = f"https://{region}.brightpearlconnect.com/public-api/{account_name}/contact-service/contact/{contactId}/postal-address"
        put_payload = [{
            "postalAddressId": postalAddressId,
            "type": "DEL"
        }]

        ok = bp_put_json(put_url, headers, put_payload, log_callback, cancel_token)
        if ok:
            conn = connect_sqlite(db_path)
            try:
                mark_rows_processed(conn, "validated_addresses", record_id, f"Linked postalAddressId {postalAddressId} to contactId {contactId}")
                conn.commit()
            finally:
                conn.close()
            log_sync(f"✅ Linked postalAddressId {postalAddressId} to contactId {contactId}", log_callback)
        else:
            log_sync(f"❌ Failed to link postalAddressId {postalAddressId} to contactId {contactId}", log_callback)

        if progress_callback:
            progress_callback(j, max(total_links, 1))
        else:
            update_progress(j, max(total_links, 1))

    log_sync("✅ Address linking complete.", log_callback)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python sync_addresses.py <account_name> <db_path>")
        sys.exit(1)

    acct = sys.argv[1]
    dbp = sys.argv[2]
    main(acct, dbp)
