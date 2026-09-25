import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class Club100Feedback(Document):
    def validate(self):
        self.set_defaults_from_session()
        self.validate_duplicate_feedback()
        self.set_submitted_at()

    def set_defaults_from_session(self):
        if not self.session or not self.member:
            return

        if not self.enrollment:
            enrollment = frappe.db.get_value(
                "Club100 Enrollment",
                {
                    "member": self.member,
                    "cohort": frappe.db.get_value(
                        "Club100 Session",
                        self.session,
                        "cohort",
                    ),
                    "status": ["in", ["Registered", "Active", "On Hold"]],
                },
                "name",
            )

            if enrollment:
                self.enrollment = enrollment

    def validate_duplicate_feedback(self):
        if not self.member or not self.session:
            return

        existing = frappe.db.exists(
            "Club100 Feedback",
            {
                "member": self.member,
                "session": self.session,
                "name": ["!=", self.name],
            },
        )

        if existing:
            frappe.throw(
                "Feedback has already been submitted for this member and session."
            )

    def set_submitted_at(self):
        if not self.submitted_at:
            self.submitted_at = now_datetime()