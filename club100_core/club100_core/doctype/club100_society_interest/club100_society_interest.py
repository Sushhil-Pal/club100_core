import hashlib
import re

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, validate_email_address

from club100_core.society.service import (
    clean_text,
    make_society_key,
    update_society_interest_counts,
)


VALID_TIMINGS = {"Morning", "Evening", "Either"}
VALID_STATUSES = {
    "Interested",
    "Contacted",
    "Confirmed",
    "Invited",
    "Converted",
    "Not Interested",
}


class Club100SocietyInterest(Document):
    def validate(self):
        self.clean_values()
        self.sync_society_snapshot()
        self.validate_required_values()
        self.validate_mobile()
        self.validate_email()
        self.validate_timing()
        self.validate_status()
        self.set_keys()
        self.set_defaults()

    def after_insert(self):
        update_society_interest_counts(self.society)

    def on_update(self):
        update_society_interest_counts(self.society)

    def after_delete(self):
        update_society_interest_counts(self.society)

    def clean_values(self):
        self.full_name = clean_text(self.full_name)
        self.mobile = normalize_mobile(self.mobile)
        self.email = clean_text(self.email).lower()
        self.society_name = clean_text(self.society_name)
        self.city_area = clean_text(self.city_area)
        self.block_tower = clean_text(self.block_tower)
        self.flat_number = clean_text(self.flat_number)
        self.preferred_timing = clean_text(self.preferred_timing)
        self.source = clean_text(self.source)
        self.page_url = clean_text(self.page_url)

    def sync_society_snapshot(self):
        if not self.society:
            return

        society = frappe.db.get_value(
            "Club100 Society",
            self.society,
            ["society_name", "city_area", "organization"],
            as_dict=True,
        )

        if not society:
            frappe.throw("Selected Club100 Society was not found.")

        self.society_name = society.society_name
        self.city_area = society.city_area

        if not self.organization and society.organization:
            self.organization = society.organization

    def validate_required_values(self):
        required = {
            "Full Name": self.full_name,
            "Mobile": self.mobile,
            "Society Name": self.society_name,
            "City / Area": self.city_area,
            "Preferred Timing": self.preferred_timing,
        }

        missing = [label for label, value in required.items() if not value]
        if missing:
            frappe.throw(f"{', '.join(missing)} is required.")

    def validate_mobile(self):
        if not re.fullmatch(r"[6-9]\d{9}", self.mobile or ""):
            frappe.throw("Please enter a valid 10-digit Indian mobile number.")

    def validate_email(self):
        if self.email and not validate_email_address(self.email):
            frappe.throw("Please enter a valid email address.")

    def validate_timing(self):
        if self.preferred_timing not in VALID_TIMINGS:
            frappe.throw("Preferred Timing must be Morning, Evening or Either.")

    def validate_status(self):
        if not self.status:
            self.status = "Interested"

        if self.status not in VALID_STATUSES:
            frappe.throw("Invalid Society Interest status.")

    def set_keys(self):
        self.society_key = make_society_key(self.society_name, self.city_area)
        self.registration_key = make_registration_key(
            self.mobile,
            self.society_name,
            self.city_area,
        )

    def set_defaults(self):
        if not self.registered_on:
            self.registered_on = now_datetime()

        if not self.source:
            self.source = "club100_website_societies"


def normalize_mobile(value):
    digits = re.sub(r"\D", "", str(value or ""))

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    return digits


def make_registration_key(mobile, society_name, city_area):
    normalized_mobile = normalize_mobile(mobile)
    society_key = make_society_key(society_name, city_area)
    raw = f"{normalized_mobile}|{society_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
