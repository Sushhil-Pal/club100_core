import frappe
from frappe.model.document import Document
from frappe.utils import getdate


class Club100Cohort(Document):
    def validate(self):
        self.set_defaults_from_program()
        self.validate_dates()
        self.validate_capacity()
        self.validate_private_cohort()
        self.validate_meeting_url()

    def set_defaults_from_program(self):
        if not self.program:
            return

        program = frappe.db.get_value(
            "Club100 Program",
            self.program,
            ["delivery_mode", "default_cohort_capacity"],
            as_dict=True,
        )

        if not program:
            return

        if not self.delivery_mode and program.delivery_mode:
            self.delivery_mode = program.delivery_mode

        if not self.capacity and program.default_cohort_capacity:
            self.capacity = program.default_cohort_capacity

    def validate_dates(self):
        if self.start_date and self.end_date:
            if getdate(self.end_date) < getdate(self.start_date):
                frappe.throw("End Date cannot be before Start Date.")

    def validate_capacity(self):
        if self.capacity <= 0:
            frappe.throw("Capacity must be greater than 0.")

    def validate_private_cohort(self):
        if self.cohort_type == "Private" and not self.organization:
            frappe.throw("Organization is required for private cohorts.")

    def validate_meeting_url(self):
        if (
            self.delivery_mode in ("Online", "Hybrid")
            and self.status in ("Open", "Active")
            and not self.default_meeting_url
        ):
            frappe.msgprint(
                "Meeting URL has not been configured for this online cohort."
            )