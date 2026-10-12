import frappe
from frappe.utils import cint

from club100_core.club100_core.doctype.club100_society_interest.club100_society_interest import (
    make_registration_key,
    normalize_mobile,
)
from club100_core.society.service import (
    clean_text,
    get_or_create_society,
    update_society_interest_counts,
)


SUCCESS_MESSAGE = (
    "Thanks — your interest has been registered. "
    "We will contact you once your onsite batch, timing and "
    "applicable pricing are confirmed."
)

EXISTS_MESSAGE = (
    "You are already registered for Society Fitness. "
    "We will keep you updated as participation in your society grows."
)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_interest(
    full_name=None,
    mobile=None,
    email=None,
    society_name=None,
    city_area=None,
    block_tower=None,
    flat_number=None,
    preferred_timing=None,
    interest_confirmation=None,
    website=None,
    source=None,
    page_url=None,
):
    """
    Public registration endpoint used by the WordPress Society page.

    Society master is found/created automatically.

    A resident is unique by:
        normalized mobile + normalized society name + normalized city/area
    """

    if clean_text(website):
        return {
            "status": "created",
            "message": SUCCESS_MESSAGE,
        }

    if not cint(interest_confirmation):
        frappe.throw(
            "Please confirm that you are interested in joining Club100 "
            "if it starts in your society."
        )

    normalized_mobile = normalize_mobile(mobile)
    cleaned_society_name = clean_text(society_name)
    cleaned_city_area = clean_text(city_area)

    registration_key = make_registration_key(
        normalized_mobile,
        cleaned_society_name,
        cleaned_city_area,
    )

    existing = frappe.db.get_value(
        "Club100 Society Interest",
        {"registration_key": registration_key},
        ["name", "society", "status"],
        as_dict=True,
    )

    if existing:
        if existing.society:
            update_society_interest_counts(existing.society)

        return {
            "status": "exists",
            "message": EXISTS_MESSAGE,
        }

    society = get_or_create_society(
        cleaned_society_name,
        cleaned_city_area,
    )

    society_values = frappe.db.get_value(
        "Club100 Society",
        society,
        ["organization"],
        as_dict=True,
    )

    doc = frappe.get_doc(
        {
            "doctype": "Club100 Society Interest",
            "full_name": full_name,
            "mobile": normalized_mobile,
            "email": email,
            "society": society,
            "society_name": cleaned_society_name,
            "organization": society_values.organization if society_values else None,
            "city_area": cleaned_city_area,
            "block_tower": block_tower,
            "flat_number": flat_number,
            "preferred_timing": preferred_timing,
            "status": "Interested",
            "source": clean_text(source) or "club100_website_societies",
            "page_url": page_url,
        }
    )

    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        return {
            "status": "exists",
            "message": EXISTS_MESSAGE,
        }

    update_society_interest_counts(society)

    return {
        "status": "created",
        "message": SUCCESS_MESSAGE,
    }


def backfill_society_links():
    """
    One-time migration helper for Society Interest records created before
    the Club100 Society master/link was introduced.
    """

    rows = frappe.get_all(
        "Club100 Society Interest",
        fields=["name", "society", "society_name", "city_area"],
        order_by="creation asc",
    )

    updated = 0
    societies_touched = set()

    for row in rows:
        society = row.society

        if not society:
            society = get_or_create_society(
                row.society_name,
                row.city_area,
            )

            frappe.db.set_value(
                "Club100 Society Interest",
                row.name,
                "society",
                society,
                update_modified=False,
            )
            updated += 1

        societies_touched.add(society)

    for society in societies_touched:
        update_society_interest_counts(society)

    return {
        "updated_interest_records": updated,
        "societies_updated": len(societies_touched),
    }
