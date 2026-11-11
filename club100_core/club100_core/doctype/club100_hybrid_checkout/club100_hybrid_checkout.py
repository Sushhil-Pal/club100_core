"""Protected accounting-pilot snapshot; no Desk mutation or public API."""
import frappe
from frappe.model.document import Document

class Club100HybridCheckout(Document):
    def validate(self):
        if not getattr(frappe.flags, 'club100_hybrid_service_write', False):
            frappe.throw('Only the internal hybrid reservation service can change this record')
        if not self.enrollment or not self.member or self.status not in ('Reserved','Captured','Released','Refunded','Review'):
            frappe.throw('Invalid hybrid checkout record')

    def on_trash(self):
        frappe.throw('Hybrid financial snapshots cannot be deleted')
