"""Company-member eligibility mapping, maintained by a System Manager."""
import hashlib
import frappe
from frappe.model.document import Document


def key_for(corporate_customer, member):
    value = f"{corporate_customer}\0{member}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Club100CorporateEmployee(Document):
    def before_insert(self):
        if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles(frappe.session.user):
            frappe.throw("System Manager required.", frappe.PermissionError)
        self.employee_key = key_for(self.corporate_customer, self.member)

    def validate(self):
        if not self.corporate_customer or not frappe.db.exists("Customer", self.corporate_customer):
            frappe.throw("Corporate Customer is required.")
        if not self.member or not frappe.db.exists("Club100 Member", self.member):
            frappe.throw("Existing Club100 Member required.")
        if self.status not in ("Active", "Inactive"):
            frappe.throw("Invalid employee status.")
        if not self.is_new():
            old = self.get_doc_before_save()
            if old and (old.corporate_customer != self.corporate_customer or old.member != self.member or old.employee_key != self.employee_key):
                frappe.throw("Cannot change corporate employee identity. Only status/notes may change.")

    def on_trash(self):
        frappe.throw("Deactivate employees instead of deleting their corporate eligibility history.")
