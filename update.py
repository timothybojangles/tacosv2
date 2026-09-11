def flatten_and_use_contact_id_as_key(data):
    element_titles = [
        "contactId", "primaryEmail", "secondaryEmail", "tertiaryEmail", "firstName", "lastName",
        "isSupplier", "companyName", "isStaff", "isCustomer", "createdOn", "updatedOn",
        "lastContactedOn", "lastOrderedOn", "nominalCode", "isPrimary", "pri", "sec", "mob",
        "exactCompanyName", "title"
    ]
    contact_catalogue = {}
    for item in data:
        item_dict = {element_titles[i]: item[i] for i in range(len(item))}
        contact_id = item_dict.get("contactId")
        if contact_id:
            contact_catalogue[contact_id] = item_dict
    return contact_catalogue

def update_contact_catalogue(account_name, db_path, log_callback=None, cancel_token=None):
    _ensure_account_binding(db_path, account_name)

    try:
        app_ref, token, region = fetch_credentials_from_sqlite(account_name, db_path)
    except ValueError as e:
        log(str(e), log_callback)
        return

    api_url_catalogue = f'https://{region}.brightpearlconnect.com/public-api/{account_name}/contact-service/contact-search'
    headers = {'brightpearl-app-ref': app_ref, 'brightpearl-account-token': token}

    first_result = 1
    more_pages_available = True
    all_contacts = []
    contact_counter = 0

    while more_pages_available:
        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after syncing {contact_counter} contacts (so far).", log_callback)
            break

        paginated_url = f"{api_url_catalogue}?pageSize=500&firstResult={first_result}"
        json_response, next_throttle_period, requests_remaining = send_request(
            paginated_url, headers, cancel_token=cancel_token, log_callback=log_callback
        )

        if cancel_token and cancel_token.is_set():
            log(f"🛑 Cancelled after request (so far: {contact_counter}).", log_callback)
            break

        if json_response:
            data = json.loads(json_response).get('response', {})
            results = data.get('results', [])
            all_contacts.extend(results)
            contact_counter += len(results)
            log(f"📦 Synced {contact_counter} contacts", log_callback)

            metadata = data.get('metaData', {})
            more_pages_available = metadata.get('morePagesAvailable', False)
            last_result = metadata.get('lastResult', first_result + 500)
            first_result = last_result + 1

            if requests_remaining < 1 and next_throttle_period > 0:
                log(f"⏳ Throttling: waiting {next_throttle_period} ms...", log_callback)
                _sleep_with_cancel_ms(next_throttle_period, cancel_token, log_callback)
        else:
            more_pages_available = False

    if not all_contacts:
        if cancel_token and cancel_token.is_set():
            # nothing to write; cancelled early
            return contact_counter
        log("⚠️ No contacts returned from Brightpearl.", log_callback)
        return contact_counter

    if cancel_token and cancel_token.is_set():
        # don't write if cancelled; return what we got
        return contact_counter

    # Write to DB
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS catalogue_contact (
            contactId INTEGER PRIMARY KEY,
            primaryEmail TEXT, secondaryEmail TEXT, tertiaryEmail TEXT,
            firstName TEXT, lastName TEXT, isSupplier BOOLEAN, companyName TEXT,
            isStaff BOOLEAN, isCustomer BOOLEAN, createdOn TEXT, updatedOn TEXT,
            lastContactedOn TEXT, lastOrderedOn TEXT, nominalCode INTEGER,
            isPrimary BOOLEAN, pri TEXT, sec TEXT, mob TEXT, exactCompanyName TEXT, title TEXT
        )
    """)
    cursor.execute("DELETE FROM contact_catalogue")

    contact_catalogue = flatten_and_use_contact_id_as_key(all_contacts)
    for contact in contact_catalogue.values():
        if cancel_token and cancel_token.is_set():
            log("🛑 Cancelled during DB write; partial commit.", log_callback)
            break
        cursor.execute("""
            INSERT INTO catalogue_contact (
                contactId, primaryEmail, secondaryEmail, tertiaryEmail, firstName, lastName,
                isSupplier, companyName, isStaff, isCustomer, createdOn, updatedOn,
                lastContactedOn, lastOrderedOn, nominalCode, isPrimary, pri, sec, mob,
                exactCompanyName, title
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            c.get("contactId"), c.get("primaryEmail"), c.get("secondaryEmail"),
            c.get("tertiaryEmail"), c.get("firstName"), c.get("lastName"),
            c.get("isSupplier"), c.get("companyName"), c.get("isStaff"), c.get("isCustomer"),
            c.get("createdOn"), c.get("updatedOn"), c.get("lastContactedOn"), c.get("lastOrderedOn"),
            c.get("nominalCode"), c.get("isPrimary"), c.get("pri"), c.get("sec"), c.get("mob"),
            c.get("exactCompanyName"), c.get("title")
        ))
    conn.commit(); conn.close()
    return contact_counter