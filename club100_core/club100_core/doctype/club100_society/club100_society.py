import frappe
from frappe.model.document import Document

from club100_core.society.service import clean_text, make_society_key


VALID_STATUSES = {
    "Interest Gathering",
    "Planning",
    "Active",
    "Inactive",
}


class Club100Society(Document):
    def validate(self):
        self.society_name = clean_text(self.society_name)
        self.city_area = clean_text(self.city_area)

        if not self.society_name:
            frappe.throw("Society Name is required.")

        if not self.city_area:
            frappe.throw("City / Area is required.")

        if not self.status:
            self.status = "Interest Gathering"

        if self.status not in VALID_STATUSES:
            frappe.throw("Invalid Society status.")

        self.society_key = make_society_key(
            self.society_name,
            self.city_area,
        )
