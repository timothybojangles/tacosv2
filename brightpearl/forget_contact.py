"""GDPR forget-contact helpers."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, Iterable

from .common import (
    Credentials,
    ensure_account_binding,
    fetch_credentials,
    log,
    log_search_progress,
    send_request,
    should_pause_for_throttle,
    sleep_with_cancel_ms,
)


def _looks_like_date(value: str) -> bool:
    if not value or len(value) != 10:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _flatten_for_forget(rows: Iterable[Iterable]) -> Dict[int, dict]:
    element_titles = [
        "contactId",
        "primaryEmail",
        "secondaryEmail",
        "tertiaryEmail",
        "firstName",
        "lastName",
        "isSupplier",
        "companyName",
        "isStaff",
        "isCustomer",
        "createdOn",
        "updatedOn",
        "lastContactedOn",
        "lastOrderedOn",
        "nominalCode",
        "isPrimary",
        "pri",
        "sec",
        "mob",
        "exactCompanyName",
        "title",
    ]
    slim: Dict[int, dict] = {}
    for row in rows:
        item = {element_titles[i]: row[i] for i in range(len(row))}
        contact_id = item.get("contactId")
        if contact_id is None:
            continue
        slim[int(contact_id)] = {
            "contactId": contact_id,
            "firstName": item.get("firstName"),
            "lastName": item.get("lastName"),
            "primaryEmail": item.get("primaryEmail"),
            "lastOrderedOn": item.get("lastOrderedOn"),
        }
    return slim


def update_forget_contacts_catalogue(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
) -> int:
    """Fetch contacts eligible for the GDPR forget workflow."""
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    from_date = (os.environ.get("BP_CONTACTS_FROM_DATE", "") or "").strip()
    to_date = (os.environ.get("BP_CONTACTS_TO_DATE", "") or "").strip()

    base_url = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "contact-service/contact-search"
    )

    query_params = ["isStaff=false", "isSupplier=false"]
    if _looks_like_date(from_date) and _looks_like_date(to_date):
        query_params.append(f"lastOrderedOn={from_date}/{to_date}")
        log(f"➡️ Fetching contacts for forget (lastOrderedOn {from_date} → {to_date})", log_callback)
    else:
        log("➡️ Fetching contacts for forget (no lastOrderedOn filter)", log_callback)

    first_result = 1
    more_pages_available = True
    all_contacts = []
    contact_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after syncing {contact_counter} contacts (so far).", log_callback)
            break

        query = "&".join(query_params + ["pageSize=500", f"firstResult={first_result}"])
        paginated_url = f"{base_url}?{query}"

        json_response, next_throttle_period, requests_remaining = send_request(
            paginated_url, credentials.headers, cancel_token=cancel_token, log_callback=log_callback
        )

        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after request (so far: {contact_counter}).", log_callback)
            break

        if json_response:
            data = json.loads(json_response).get("response", {})
            results = data.get("results", [])
            all_contacts.extend(results)
            contact_counter += len(results)
            log(f"📦 Synced {contact_counter} contacts (forget_candidates)", log_callback)

            metadata = data.get("metaData", {})
            log_search_progress(metadata, log_callback)
            more_pages_available = metadata.get("morePagesAvailable", False)
            last_result = metadata.get("lastResult", first_result + 500)
            first_result = last_result + 1

            if should_pause_for_throttle(requests_remaining, next_throttle_period):
                log(f"⏳ Throttling: waiting {next_throttle_period} ms...", log_callback)
                sleep_with_cancel_ms(next_throttle_period, cancel_token, log_callback)
        else:
            more_pages_available = False

    if not all_contacts:
        if cancel_token and cancel_token.is_set():
            return contact_counter
        log("⚠️ No contacts returned from Brightpearl for forget.", log_callback)
        return contact_counter

    if cancel_token and cancel_token.is_set():
        return contact_counter

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS forget_contacts (
            contactId INTEGER PRIMARY KEY,
            firstName TEXT,
            lastName TEXT,
            primaryEmail TEXT,
            lastOrderedOn TEXT,
            forgot INTEGER DEFAULT 0,
            forgot_at TEXT,
            forgot_order INTEGER DEFAULT 0,
            forgot_order_at TEXT
        )
        """
    )
    cursor.execute("DELETE FROM forget_contacts")

    slim_catalogue = _flatten_for_forget(all_contacts)
    for contact in slim_catalogue.values():
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancelled during DB write; partial commit.", log_callback)
            break
        cursor.execute(
            """
            INSERT INTO forget_contacts (
                contactId,
                firstName,
                lastName,
                primaryEmail,
                lastOrderedOn,
                forgot,
                forgot_at,
                forgot_order,
                forgot_order_at
            ) VALUES (?, ?, ?, ?, ?, 0, NULL, 0, NULL)
            """,
            (
                contact.get("contactId"),
                contact.get("firstName"),
                contact.get("lastName"),
                contact.get("primaryEmail"),
                contact.get("lastOrderedOn"),
            ),
        )

    conn.commit()
    conn.close()

    return contact_counter
