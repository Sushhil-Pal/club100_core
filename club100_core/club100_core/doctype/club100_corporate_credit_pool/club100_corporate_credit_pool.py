"""Verified, immutable employer funding tranche. Creation is a finance/admin action."""
import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime
from club100_core.wallet.logic import money


def _is_manager():
    return frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles(frappe.session.user)


class Club100CorporateCreditPool(Document):
    def before_insert(self):
        if not _is_manager():
            frappe.throw("Only a System Manager can confirm employer funding.", frappe.PermissionError)
        self.amount = money(self.amount)
        if not self.funding_event_key:
            from club100_core.wallet.admin_refs import pool_event_key
            self.funding_event_key = pool_event_key(self.corporate_customer, self.funding_reference)
        # A new pool is a funding request, never confirmation of cash or GL.
        self.verified_by = None
        self.verified_at = None

    def validate(self):
        if not self.is_new():
            frappe.throw("A funded pool is immutable. Create another tranche for additional funding.")
        if not _is_manager():
            frappe.throw("System Manager required.", frappe.PermissionError)
        if self.currency != "INR" or money(self.amount) <= 0:
            frappe.throw("Pool requires a positive INR funding amount.")
        if not self.corporate_customer or not frappe.db.exists("Customer", self.corporate_customer):
            frappe.throw("Select an existing ERPNext Customer.")
        if not (self.funding_event_key or "").strip() or not (self.funding_reference or "").strip():
            frappe.throw("Unique funding key and verified payment reference are required.")
        # Also catch older pools that used manually supplied internal keys.
        # New pools additionally have deterministic, unique funding keys.
        if frappe.db.exists("Club100 Corporate Credit Pool", {
            "corporate_customer": self.corporate_customer,
            "funding_reference": self.funding_reference,
        }):
            frappe.throw("A corporate funding pool already exists for this payment reference.")
        if self.funding_verified or self.status != "Awaiting Funding":
            frappe.throw("Create the pool as Awaiting Funding. Use Confirm Funding after the submitted receipt is verified and GL has posted.")

    def on_trash(self):
        frappe.throw("Corporate funding pools cannot be deleted. Preserve the audit trail.")
