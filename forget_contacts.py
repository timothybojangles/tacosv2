# utils/forget_contacts.py
# Python 3.9+ friendly, PyInstaller-safe (no f-strings in log emoji if you don't want them, but it's fine)

import os
import sys
import csv
import time
import sqlite3
from datetime import datetime
from typing import Callable, List, Dict, Optional, Tuple

import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from brightpearl.common import fetch_credentials, log_payload_exchange
from brightpearl.paths import get_data_db_path
from brightpearl.settings import get_settings
from brightpearl.performance import estimate_record_count, record_api_update
urllib3.disable_warnings(InsecureRequestWarning)

MAX_RETRIES = 3
SLEEP_BETWEEN_OK = 0.2          # seconds
FORGET_BODY = {
    "ticketsAndActivities": True,
    "notes": True,
    "customFields": True,
    "keepCityAndState": True
}

# --------------------
# Helpers: DB + creds
# --------------------

def ensure_forgot_columns(account_name: str) -> None:
    """
    Make sure the per-account data DB has:
      forgot INTEGER DEFAULT 0
      forgot_at TEXT
      forgot_order INTEGER DEFAULT 0
      forgot_order_at TEXT
    in forget_contacts.
    """
    db_path = get_data_db_path(account_name)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(forget_contacts)")
        cols = {row[1] for row in cur.fetchall()}

        if "forgot" not in cols:
            cur.execute("ALTER TABLE forget_contacts ADD COLUMN forgot INTEGER DEFAULT 0")
        if "forgot_at" not in cols:
            cur.execute("ALTER TABLE forget_contacts ADD COLUMN forgot_at TEXT")
        if "forgot_order" not in cols:
            cur.execute("ALTER TABLE forget_contacts ADD COLUMN forgot_order INTEGER DEFAULT 0")
        if "forgot_order_at" not in cols:
            cur.execute("ALTER TABLE forget_contacts ADD COLUMN forgot_order_at TEXT")

        conn.commit()
    finally:
        conn.close()

def load_contact_ids_to_forget(account_name: str) -> List[int]:
    """
    Return all contactIds where forgot is NULL/0.
    """
    db_path = get_data_db_path(account_name)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT contactId
            FROM forget_contacts
            WHERE COALESCE(forgot,0)=0
            ORDER BY contactId
        """)
        ids = [r[0] for r in cur.fetchall() if r and r[0] is not None]
    finally:
        conn.close()
    return ids

def mark_forgot(account_name: str, contact_id: int) -> None:
    """
    After a successful forget POST, stamp forgot=1 and forgot_at=now.
    """
    db_path = get_data_db_path(account_name)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE forget_contacts
               SET forgot = 1,
                   forgot_at = ?
             WHERE contactId = ?
        """, (datetime.utcnow().isoformat(timespec="seconds") + "Z", contact_id))
        conn.commit()
    finally:
        conn.close()

def load_contact_ids_to_forget_orders(account_name: str) -> List[int]:
    """
    Return all contactIds where forgot_order is NULL/0.
    """
    db_path = get_data_db_path(account_name)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT contactId
            FROM forget_contacts
            WHERE COALESCE(forgot_order,0)=0
            ORDER BY contactId
        """)
        ids = [r[0] for r in cur.fetchall() if r and r[0] is not None]
    finally:
        conn.close()
    return ids

def mark_forgot_order(account_name: str, contact_id: int) -> None:
    """
    After a successful order-forget call, stamp forgot_order=1 and forgot_order_at=now.
    """
    db_path = get_data_db_path(account_name)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE forget_contacts
               SET forgot_order = 1,
                   forgot_order_at = ?
             WHERE contactId = ?
        """, (datetime.utcnow().isoformat(timespec="seconds") + "Z", contact_id))
        conn.commit()
    finally:
        conn.close()

# --------------------
# HTTP logic
# --------------------

def _sleep_with_log(ms: int,
                    log_callback: Optional[Callable[[str], None]] = None):
    if log_callback:
        log_callback(f"⏳ Throttled, sleeping {ms} ms…")
    time.sleep(ms / 1000.0)

def _request_with_retries(url: str,
                          headers: Dict[str, str],
                          json_body: Optional[Dict] = None,
                          log_callback: Optional[Callable[[str], None]] = None,
                          cancel_token=None) -> Tuple[bool, Optional[requests.Response]]:
    """
    POST forget with retries + Brightpearl throttling.
    Returns (ok, resp)
    """

    for attempt in range(1, MAX_RETRIES + 1):
        if cancel_token and cancel_token.is_set():
            if log_callback:
                log_callback("🛑 Cancel requested before sending request.")
            return False, None

        try:
            if json_body is None:
                resp = requests.post(url, headers=headers, timeout=60, verify=False)
            else:
                resp = requests.post(url, headers=headers, json=json_body,
                                     timeout=60, verify=False)
            log_payload_exchange("POST", url, json_body, resp, log_callback)
            status = resp.status_code

            # Throttle hints (even on 200 sometimes)
            try:
                rem = int(resp.headers.get("brightpearl-requests-remaining", "3") or "3")
            except ValueError:
                rem = 3
            try:
                thr = int(resp.headers.get("brightpearl-next-throttle-period", "0") or "0")
            except ValueError:
                thr = 0

            if status == 200:
                record_api_update(estimate_record_count(json_body or {}))
                # respect throttle for next loop
                if rem <= get_settings().throttle_threshold and thr > 0:
                    _sleep_with_log(thr, log_callback=log_callback)
                else:
                    time.sleep(SLEEP_BETWEEN_OK)
                return True, resp

            # Retryable codes
            if status in (429, 502, 503, 504):
                wait_ms = thr if thr > 0 else attempt * 1000
                if log_callback:
                    log_callback(f"🔁 Retryable {status} (attempt {attempt}/{MAX_RETRIES}); sleeping {wait_ms} ms")
                time.sleep(wait_ms / 1000.0)
                continue

            # Non-retryable
            if log_callback:
                err_preview = resp.text[:400] if resp.text else "(no body)"
                log_callback(f"❌ Non-200 {status}: {err_preview}")
            return False, resp

        except requests.RequestException as e:
            if log_callback:
                log_callback(f"⚠️ Request exception on attempt {attempt}/{MAX_RETRIES}: {e}")
            time.sleep(attempt * 1.0)

    return False, None

# --------------------
# Failure CSV output
# --------------------

def _write_failed_csv(account_name: str,
                      failures: List[Dict[str, str]],
                      log_callback: Optional[Callable[[str], None]] = None):
    if not failures:
        return
    os.makedirs("output", exist_ok=True)
    out_path = os.path.join(
        "output",
        f"{account_name}_failed_forget_contacts_{int(time.time())}.csv"
    )
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["contactId", "status", "error"])
        writer.writeheader()
        for row in failures:
            writer.writerow(row)

    if log_callback:
        log_callback(f"📄 Wrote {len(failures)} failures → {out_path}")

# --------------------
# Public entry point
# --------------------

def run_forget_contacts(
    account_name: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_token=None
):
    """
    Main callable for the GUI thread wrapper in main.py.

    - Uses account_name to look up creds in credentials.db
    - Uses {account}_brightpearl_data.db for contact list + update
    - Iterates through each contactId where forgot=0
    - Calls Brightpearl /forget
    - Updates progress_callback(done, total)
    - Streams log_callback("text...")
    """

    # fetch creds
    try:
        credentials = fetch_credentials(account_name)
        app_ref, token, region = credentials.app_ref, credentials.token, credentials.region
    except Exception as e:
        if log_callback:
            log_callback("❌ " + str(e))
        return

    ensure_forgot_columns(account_name)

    contact_ids = load_contact_ids_to_forget(account_name)
    total = len(contact_ids)

    if total == 0:
        if log_callback:
            log_callback("ℹ️ No contacts pending forget (all already flagged).")
        if progress_callback:
            progress_callback(total, max(total, 1))  # 100%
        return

    headers = {
        "brightpearl-app-ref": app_ref,
        "brightpearl-account-token": token,
        "Content-Type": "application/json",
    }

    if log_callback:
        log_callback(f"🚨 Starting GDPR forget for {total} contact(s)…")

    failed_rows: List[Dict[str, str]] = []
    done = 0

    for cid in contact_ids:
        if cancel_token and cancel_token.is_set():
            if log_callback:
                log_callback("🛑 Cancel requested. Stopping forget loop.")
            break

        url = (
            f"https://{region}.brightpearlconnect.com/public-api/"
            f"{account_name}/contact-service/contact/{cid}/forget"
        )

        ok, resp = _request_with_retries(
            url,
            headers,
            FORGET_BODY,
            log_callback=log_callback,
            cancel_token=cancel_token
        )

        if ok:
            try:
                mark_forgot(account_name, cid)
                if log_callback:
                    log_callback(f"✅ Forgot contactId {cid}")
            except Exception as e:
                if log_callback:
                    log_callback(f"⚠️ Forgot OK but DB update failed for {cid}: {e}")
        else:
            status = resp.status_code if resp is not None else "no-response"
            # preview up to 400 chars
            err_txt = ""
            if resp is not None and getattr(resp, "text", None):
                err_txt = resp.text[:400]
            else:
                err_txt = "request failed"
            failed_rows.append({
                "contactId": str(cid),
                "status": str(status),
                "error": err_txt
            })
            if log_callback:
                log_callback(f"❌ Failed to forget {cid} ({status})")

        done += 1
        if progress_callback:
            progress_callback(done, total)

    # save any failures
    if failed_rows:
        _write_failed_csv(account_name, failed_rows, log_callback=log_callback)
        if log_callback:
            log_callback(f"⚠️ Completed with {len(failed_rows)} failure(s).")
    else:
        if log_callback:
            log_callback("🎉 All processed contacts successfully forgotten (or operation cancelled).")

    # if user cancelled mid-way, we still return gracefully
    return


def run_forget_contact_orders(
    account_name: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_token=None
):
    """
    Calls order forget-contact endpoint for each contact with forgot_order=0.
    """
    try:
        credentials = fetch_credentials(account_name)
        app_ref, token, region = credentials.app_ref, credentials.token, credentials.region
    except Exception as e:
        if log_callback:
            log_callback("❌ " + str(e))
        return

    ensure_forgot_columns(account_name)

    contact_ids = load_contact_ids_to_forget_orders(account_name)
    total = len(contact_ids)

    if total == 0:
        if log_callback:
            log_callback("ℹ️ No contacts pending order forget (all already flagged).")
        if progress_callback:
            progress_callback(total, max(total, 1))
        return

    headers = {
        "brightpearl-app-ref": app_ref,
        "brightpearl-account-token": token,
        "Content-Type": "application/json",
    }

    if log_callback:
        log_callback(f"🚨 Starting order contact forget for {total} contact(s)…")

    failed_rows: List[Dict[str, str]] = []
    done = 0

    for cid in contact_ids:
        if cancel_token and cancel_token.is_set():
            if log_callback:
                log_callback("🛑 Cancel requested. Stopping order forget loop.")
            break

        url = (
            f"https://{region}.brightpearlconnect.com/public-api/"
            f"{account_name}/order-service/order/forget-contact/{cid}?keepCityAndState=true"
        )

        ok, resp = _request_with_retries(
            url,
            headers,
            log_callback=log_callback,
            cancel_token=cancel_token
        )

        if ok:
            try:
                mark_forgot_order(account_name, cid)
                if log_callback:
                    log_callback(f"✅ Removed contactId {cid} from orders")
            except Exception as e:
                if log_callback:
                    log_callback(f"⚠️ Order forget OK but DB update failed for {cid}: {e}")
        else:
            status = resp.status_code if resp is not None else "no-response"
            if resp is not None and getattr(resp, "text", None):
                err_txt = resp.text[:400]
            else:
                err_txt = "request failed"
            failed_rows.append({
                "contactId": str(cid),
                "status": str(status),
                "error": err_txt
            })
            if log_callback:
                log_callback(f"❌ Failed to remove from orders {cid} ({status})")

        done += 1
        if progress_callback:
            progress_callback(done, total)

    if failed_rows:
        _write_failed_csv(account_name, failed_rows, log_callback=log_callback)
        if log_callback:
            log_callback(f"⚠️ Completed order forget with {len(failed_rows)} failure(s).")
    else:
        if log_callback:
            log_callback("🎉 All processed contacts were removed from orders (or operation cancelled).")

    return
