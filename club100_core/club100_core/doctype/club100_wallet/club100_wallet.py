import frappe
from frappe.model.document import Document


class Club100Wallet(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("Wallet records cannot be edited directly; use the trusted wallet service.")
        if self.currency != "INR":
            frappe.throw("Only INR wallets are supported.")
        if self.status != "Active":
            frappe.throw("New wallets must be active.")

    def on_trash(self):
        frappe.throw("Wallets cannot be deleted; preserve the audit trail.")
