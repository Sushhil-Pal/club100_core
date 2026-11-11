"""Internal accounting link record. Cannot be edited from ERPNext Desk."""
import frappe
from frappe.model.document import Document


class Club100MembershipAccounting(Document):
    def validate(self):
        if not getattr(frappe.flags, 'club100_accounting_internal', False):
            frappe.throw('Membership accounting links are managed by the accounting service.')
        if self.currency != 'INR' or not self.enrollment or not self.member:
            frappe.throw('Valid INR enrollment and member required')

    def on_trash(self):
        frappe.throw('Accounting link records cannot be deleted')
