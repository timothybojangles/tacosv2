"""Additional address / contact catalogue helpers."""
from __future__ import annotations

import json
import sqlite3
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
from .performance import record_api_update
from .settings import get_download_retry_settings


def _flatten_contact_rows(rows: Iterable[Iterable]) -> Dict[int, dict]:
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
    catalogue: Dict[int, dict] = {}
    for row in rows:
        item = {element_titles[i]: row[i] for i in range(len(row))}
        contact_id = item.get("contactId")
        if contact_id is not None:
            catalogue[int(contact_id)] = item
    return catalogue


def update_contact_catalogue(
    account_name: str,
    db_path: str,
    log_callback=None,
    cancel_token=None,
    append_mode: bool = False,
    append_first_result: int | None = None,
) -> int:
    """Fetch the contact catalogue used to enrich address imports."""
    ensure_account_binding(db_path, account_name)

    try:
        credentials: Credentials = fetch_credentials(account_name)
    except ValueError as exc:
        log(str(exc), log_callback)
        return 0

    api_url_catalogue = (
        f"https://{credentials.region}.brightpearlconnect.com/public-api/{account_name}/"
        "contact-service/contact-search"
    )

    append_enabled = bool(append_mode)
    if append_enabled:
        try:
            first_result = int(append_first_result)
        except (TypeError, ValueError):
            first_result = 0

        if first_result < 1:
            log(
                "❌ Append? is set to Yes, but firstResult is not set to a positive integer.",
                log_callback,
            )
            return 0
        log(
            f"ℹ️ Append? Yes — preserving existing contact_catalogue rows and starting from firstResult={first_result}.",
            log_callback,
        )
    else:
        first_result = 1

    more_pages_available = True
    all_contacts = []
    contact_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after syncing {contact_counter} contacts (so far).", log_callback)
            break

        paginated_url = f"{api_url_catalogue}?pageSize=500&firstResult={first_result}"
        json_response = None
        next_throttle_period = 0
        requests_remaining = 0
        max_retries, default_sleep_ms = get_download_retry_settings()
        for attempt in range(1, max_retries + 1):
            if cancel_token and cancel_token.is_set():
                break

            json_response, next_throttle_period, requests_remaining = send_request(
                paginated_url,
                credentials.headers,
                cancel_token=cancel_token,
                log_callback=log_callback,
            )

            if json_response:
                break

            if attempt < max_retries:
                retry_sleep_ms = default_sleep_ms * attempt
                log(
                    (
                        f"⚠️ Contact catalogue request failed (attempt {attempt}/{max_retries}); "
                        f"retrying in {retry_sleep_ms} ms..."
                    ),
                    log_callback,
                )
                sleep_with_cancel_ms(retry_sleep_ms, cancel_token, log_callback)

        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after request (so far: {contact_counter}).", log_callback)
            break

        if json_response:
            data = json.loads(json_response).get("response", {})
            results = data.get("results", [])
            all_contacts.extend(results)
            contact_counter += len(results)
            record_api_update(len(results))
            log(f"📦 Synced {contact_counter} contacts", log_callback)

            metadata = data.get("metaData", {})
            log_search_progress(metadata, log_callback)
            more_pages_available = metadata.get("morePagesAvailable", False)
            last_result = metadata.get("lastResult", first_result + 500)
            first_result = last_result + 1

            if should_pause_for_throttle(requests_remaining, next_throttle_period):
                log(f"⏳ Throttling: waiting {next_throttle_period} ms...", log_callback)
                sleep_with_cancel_ms(next_throttle_period, cancel_token, log_callback)
        else:
            log(
                f"❌ Failed to fetch contact catalogue page after {max_retries} attempts: {paginated_url}",
                log_callback,
            )
            more_pages_available = False

    if not all_contacts:
        if cancel_token and cancel_token.is_set():
            return contact_counter
        log("⚠️ No contacts returned from Brightpearl.", log_callback)
        return contact_counter

    if cancel_token and cancel_token.is_set():
        return contact_counter

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_catalogue (
            contactId INTEGER PRIMARY KEY,
            primaryEmail TEXT,
            secondaryEmail TEXT,
            tertiaryEmail TEXT,
            firstName TEXT,
            lastName TEXT,
            isSupplier BOOLEAN,
            companyName TEXT,
            isStaff BOOLEAN,
            isCustomer BOOLEAN,
            createdOn TEXT,
            updatedOn TEXT,
            lastContactedOn TEXT,
            lastOrderedOn TEXT,
            nominalCode INTEGER,
            isPrimary BOOLEAN,
            pri TEXT,
            sec TEXT,
            mob TEXT,
            exactCompanyName TEXT,
            title TEXT
        )
        """
    )
    if append_enabled:
        log("ℹ️ Append mode enabled; existing contact_catalogue rows will be kept.", log_callback)
    else:
        cursor.execute("DELETE FROM contact_catalogue")

    contact_catalogue = _flatten_contact_rows(all_contacts)
    for contact in contact_catalogue.values():
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancelled during DB write; partial commit.", log_callback)
            break
        cursor.execute(
            """
            INSERT OR IGNORE INTO contact_catalogue (
                contactId,
                primaryEmail,
                secondaryEmail,
                tertiaryEmail,
                firstName,
                lastName,
                isSupplier,
                companyName,
                isStaff,
                isCustomer,
                createdOn,
                updatedOn,
                lastContactedOn,
                lastOrderedOn,
                nominalCode,
                isPrimary,
                pri,
                sec,
                mob,
                exactCompanyName,
                title
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contact.get("contactId"),
                contact.get("primaryEmail"),
                contact.get("secondaryEmail"),
                contact.get("tertiaryEmail"),
                contact.get("firstName"),
                contact.get("lastName"),
                contact.get("isSupplier"),
                contact.get("companyName"),
                contact.get("isStaff"),
                contact.get("isCustomer"),
                contact.get("createdOn"),
                contact.get("updatedOn"),
                contact.get("lastContactedOn"),
                contact.get("lastOrderedOn"),
                contact.get("nominalCode"),
                contact.get("isPrimary"),
                contact.get("pri"),
                contact.get("sec"),
                contact.get("mob"),
                contact.get("exactCompanyName"),
                contact.get("title"),
            ),
        )
    conn.commit()
    conn.close()
    return contact_counter
