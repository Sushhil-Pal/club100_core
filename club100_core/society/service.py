import re

import frappe


def clean_text(value):
    if value is None:
        return ""

    return " ".join(str(value).strip().split())


def normalized_key_text(value):
    value = clean_text(value).lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def make_society_key(society_name, city_area):
    society = normalized_key_text(society_name)
    area = normalized_key_text(city_area)

    if not society or not area:
        return ""

    return f"{society}|{area}"


def get_or_create_society(society_name, city_area):
    society_name = clean_text(society_name)
    city_area = clean_text(city_area)

    if not society_name:
        frappe.throw("Society Name is required.")

    if not city_area:
        frappe.throw("City / Area is required.")

    society_key = make_society_key(society_name, city_area)

    existing = frappe.db.get_value(
        "Club100 Society",
        {"society_key": society_key},
        "name",
    )

    if existing:
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "Club100 Society",
            "society_name": society_name,
            "city_area": city_area,
            "status": "Interest Gathering",
            "society_key": society_key,
        }
    )

    try:
        doc.insert(ignore_permissions=True)
        return doc.name
    except frappe.DuplicateEntryError:
        return frappe.db.get_value(
            "Club100 Society",
            {"society_key": society_key},
            "name",
        )


def update_society_interest_counts(society):
    if not society or not frappe.db.exists("Club100 Society", society):
        return

    open_statuses = ["Interested", "Contacted", "Confirmed", "Invited"]
    confirmed_statuses = ["Confirmed", "Invited"]

    open_interest_count = frappe.db.count(
        "Club100 Society Interest",
        {
            "society": society,
            "status": ["in", open_statuses],
        },
    )

    confirmed_interest_count = frappe.db.count(
        "Club100 Society Interest",
        {
            "society": society,
            "status": ["in", confirmed_statuses],
        },
    )

    frappe.db.set_value(
        "Club100 Society",
        society,
        {
            "open_interest_count": open_interest_count,
            "confirmed_interest_count": confirmed_interest_count,
        },
        update_modified=False,
    )
