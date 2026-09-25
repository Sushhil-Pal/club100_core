import frappe
from frappe.model.document import Document
from frappe.utils import getdate


class Club100Enrollment(Document):
    def validate(self):
        self.set_defaults_from_member()
        self.validate_dates()
        self.validate_sponsorship()
        self.validate_duplicate_active_enrollment()

    def set_defaults_from_member(self):
        if not self.member:
            return

        member = frappe.db.get_value(
            "Club100 Member",
            self.member,
            ["organization", "current_fitness_level"],
            as_dict=True,
        )

        if not member:
            return

        if not self.organization and member.organization:
            self.organization = member.organization

        if not self.starting_fitness_level and member.current_fitness_level:
            self.starting_fitness_level = member.current_fitness_level

        if not self.current_fitness_level and self.starting_fitness_level:
            self.current_fitness_level = self.starting_fitness_level

    def validate_dates(self):
        if self.start_date and self.end_date:
            if getdate(self.end_date) < getdate(self.start_date):
                frappe.throw("End Date cannot be before Start Date.")

    def validate_sponsorship(self):
        if self.enrollment_type == "Sponsored" and not self.sponsored_by:
            frappe.throw("Sponsored By is required for sponsored enrollments.")

    def validate_duplicate_active_enrollment(self):
        if not self.member or not self.program:
            return

        existing = frappe.db.exists(
            "Club100 Enrollment",
            {
                "member": self.member,
                "program": self.program,
                "status": ["in", ["Registered", "Active", "On Hold"]],
                "name": ["!=", self.name],
            },
        )

        if existing:
            frappe.throw(
                "This member already has an active enrollment for this program."
            )