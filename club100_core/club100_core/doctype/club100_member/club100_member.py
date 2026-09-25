import frappe
from frappe.model.document import Document


class Club100Member(Document):
    def validate(self):
        self.set_full_name()
        self.validate_organization()

    def set_full_name(self):
        names = [self.first_name, self.last_name]
        self.full_name = " ".join(name for name in names if name)

    def validate_organization(self):
        if self.member_type in ("Corporate", "Society") and not self.organization:
            frappe.throw(
                f"Organization is required for {self.member_type} members."
            )