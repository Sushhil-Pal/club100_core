"""Immutable source-to-GL evidence. Financial writes only through accounting service."""
import frappe
from frappe.model.document import Document


class Club100WalletAccountingEvent(Document):
    def validate(self):
        if not getattr(frappe.flags, 'club100_wallet_accounting_internal', False):
            frappe.throw('Wallet accounting evidence is system-managed')
        if not self.event_key or self.company == '' or self.currency != 'INR':
            frappe.throw('Immutable INR event identity and company required')
        if self.docstatus or self.is_new() is False:
            frappe.throw('Posted wallet accounting evidence cannot be edited')

    def on_trash(self):
        frappe.throw('Wallet accounting evidence cannot be deleted')
