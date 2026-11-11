"""Fail-closed, service-owned payment-evidence inbox."""
import frappe
from frappe.model.document import Document


class Club100HybridPaymentEvidence(Document):
    def validate(self):
        if not getattr(frappe.flags, 'club100_hybrid_evidence_write', False):
            frappe.throw('Only internal verified payment-evidence service may write')
        if (self.provider_status != 'captured' or self.state not in
              ('Verified Pending Invoice','Manual Review','Settled')):
            frappe.throw('Verified captured provider status required')
        if self.origin not in ('Browser','Webhook') or self.evidence_version != 'D2C2C-v1':
            frappe.throw('Invalid evidence source/version')

    def on_trash(self):
        frappe.throw('Verified payment evidence must not be deleted')
