import frappe
from frappe.model.document import Document


class Club100WalletReservation(Document):
    def validate(self):
        if not getattr(frappe.flags, "club100_wallet_transaction", False):
            frappe.throw("Wallet reservations can only be managed by the trusted wallet service.")
        if self.currency != "INR" or not self.items:
            frappe.throw("A reservation requires INR and at least one grant allocation.")

    def on_trash(self):
        frappe.throw("Wallet reservations cannot be deleted; retain transaction history.")
