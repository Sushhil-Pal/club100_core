import frappe
from frappe.model.document import Document

from club100_core.wallet.core import money


class Club100WalletLedger(Document):
    def before_insert(self):
        if not getattr(frappe.flags, "club100_wallet_internal_post", False):
            frappe.throw("Ledger is append-only and can only be written by the wallet service.")

    def validate(self):
        if not self.is_new():
            frappe.throw("Ledger entries cannot be modified.")
        if self.currency != "INR":
            frappe.throw("Only INR wallet entries are supported.")
        amt = money(self.amount)
        if not amt:
            frappe.throw("Ledger amount cannot be zero.")
        if self.entry_type in ("Credit", "Refund") and amt <= 0:
            frappe.throw("Credit and Refund ledger entries must be positive.")
        if self.entry_type in ("Spend", "Reversal", "Expiry") and amt >= 0:
            frappe.throw("Spend, Reversal and Expiry ledger entries must be negative.")

    def on_trash(self):
        frappe.throw("Ledger entries cannot be deleted.")
