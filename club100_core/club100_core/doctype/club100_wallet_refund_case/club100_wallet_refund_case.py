import frappe
from frappe.model.document import Document


class Club100WalletRefundCase(Document):
    def validate(self):
        if not getattr(frappe.flags, 'club100_recovery_write', False):
            frappe.throw('Refund cases can only be changed by the trusted recovery service.')
        if self.currency != 'INR' or not self.enrollment:
            frappe.throw('Refund case requires a linked enrollment and INR.')

    def on_trash(self):
        frappe.throw('Refund cases are audit records and cannot be deleted.')
