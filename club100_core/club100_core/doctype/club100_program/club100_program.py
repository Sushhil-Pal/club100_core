import frappe
from frappe.model.document import Document


class Club100Program(Document):
    def validate(self):
        self.validate_numbers()
        self.validate_price()

    def validate_numbers(self):
        if self.duration_weeks <= 0:
            frappe.throw("Duration Weeks must be greater than 0.")

        if self.sessions_per_week <= 0:
            frappe.throw("Sessions Per Week must be greater than 0.")

        if self.session_duration_minutes <= 0:
            frappe.throw("Session Duration Minutes must be greater than 0.")

        if self.default_cohort_capacity <= 0:
            frappe.throw("Default Cohort Capacity must be greater than 0.")

    def validate_price(self):
        if self.payment_required and not self.standard_price:
            frappe.throw(
                "Standard Price is required when Payment Required is enabled."
            )