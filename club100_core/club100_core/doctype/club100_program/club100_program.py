import frappe
from frappe.model.document import Document


class Club100Program(Document):
    def validate(self):
        self.validate_numbers()
        self.validate_reassessment_interval()
        self.validate_price()

    def validate_numbers(self):
        if self.duration_weeks and self.duration_weeks <= 0:
            frappe.throw("Duration Weeks must be greater than 0.")

        if self.sessions_per_week <= 0:
            frappe.throw("Sessions Per Week must be greater than 0.")

        if self.session_duration_minutes <= 0:
            frappe.throw("Session Duration Minutes must be greater than 0.")

        if self.default_cohort_capacity <= 0:
            frappe.throw("Default Cohort Capacity must be greater than 0.")

    def validate_reassessment_interval(self):
        interval = int(
            self.reassessment_interval_months or 0
        )

        if interval < 0:
            frappe.throw(
                "Reassessment Interval (Months) cannot be negative."
            )

        if self.reassessment_included and interval <= 0:
            frappe.throw(
                "Reassessment Interval (Months) must be greater than 0 "
                "when Reassessment Included is enabled."
            )

    def validate_price(self):
        """
        Program pricing may be determined by Plan + Membership Duration.

        standard_price therefore remains optional. If provided, it must
        still be a valid non-negative amount.
        """
        if self.standard_price is not None and self.standard_price < 0:
            frappe.throw("Standard Price cannot be negative.")
