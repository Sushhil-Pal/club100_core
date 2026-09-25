import frappe
from frappe.model.document import Document


class Club100Assessment(Document):
    def validate(self):
        self.set_defaults_from_enrollment()
        self.validate_completed_assessment()

    def set_defaults_from_enrollment(self):
        if not self.enrollment:
            return

        enrollment = frappe.db.get_value(
            "Club100 Enrollment",
            self.enrollment,
            ["member", "program"],
            as_dict=True,
        )

        if not enrollment:
            return

        if not self.member and enrollment.member:
            self.member = enrollment.member

        if not self.program and enrollment.program:
            self.program = enrollment.program

    def validate_completed_assessment(self):
        if self.status != "Completed":
            return

        if not self.metrics:
            frappe.throw(
                "At least one assessment metric is required before completing the assessment."
            )

        if self.fitness_score is None:
            frappe.throw(
                "Fitness Score is required before completing the assessment."
            )