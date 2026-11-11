"""Append-only reward identity with controlled, audited settlement transitions."""
from decimal import Decimal
import frappe
from frappe.model.document import Document

IMMUTABLE = (
    'event_key', 'attribution', 'partner', 'referred_member', 'enrollment',
    'payment_reference', 'reward_track', 'reward_event', 'basis_amount',
    'rate', 'reward_amount', 'currency', 'auto_tax_rate_percent',
)
FINANCE = (
    'basis_verification_status', 'verified_basis_amount', 'verified_reward_amount',
    'finance_proof_reference', 'finance_verified_by', 'finance_verified_on',
    'eligible_on', 'credit_validity_type', 'credit_expires_on', 'basis_evidence_type',
)
ALLOWED = {
    'On Hold': {'Approved', 'Reversed'},
    'Pending': {'Reversed'},  # legacy records cannot be approved without new payment basis
    'Approved': {'Available', 'Reversed'},
    'Available': {'Reversed'},
    'Redeemed': set(), 'Paid': set(), 'Reversed': set(),
}


def _same(a, b, field):
    if field in ('basis_amount', 'rate', 'reward_amount',
                 'verified_basis_amount', 'verified_reward_amount'):
        return Decimal(str(a or 0)) == Decimal(str(b or 0))
    return str(a or '') == str(b or '')


class Club100ReferralReward(Document):
    def validate(self):
        if self.is_new():
            if self.status not in ('On Hold', 'Pending'):
                frappe.throw('New rewards must start On Hold; no direct settlement allowed')
            if self.get('basis_verification_status') not in ('', None, 'Unverified'):
                frappe.throw('New rewards cannot claim a verified basis')
            return
        old = frappe.get_doc(self.doctype, self.name)
        for field in IMMUTABLE:
            if not _same(old.get(field), self.get(field), field):
                frappe.throw(f'Reward financial field {field} is immutable')
        touched = any(not _same(old.get(f), self.get(f), f)
                      for f in (*FINANCE, 'status', 'settlement_reference', 'settled_on', 'notes'))
        if touched and not getattr(frappe.flags, 'club100_referral_settlement_write', False):
            frappe.throw('Referral financial transitions require trusted settlement service')
        if self.status != old.status and self.status not in ALLOWED.get(old.status, set()):
            frappe.throw('Unsupported reward lifecycle transition')
        if old.status in ('Approved', 'Available', 'Reversed', 'Redeemed', 'Paid'):
            for field in FINANCE:
                if not _same(old.get(field), self.get(field), field):
                    frappe.throw(f'Verified reward field {field} cannot be revised')
        if self.status in ('Approved', 'Available'):
            from club100_core.referrals.settlement_logic import money
            if (self.basis_verification_status != 'Verified'
                    or money(self.verified_basis_amount or 0) <= 0
                    or money(self.verified_reward_amount or 0) <= 0
                    or not self.finance_proof_reference or not self.eligible_on):
                frappe.throw('Verified basis, proof, reward amount and maturity required')
        if self.status == 'Available' and (self.reward_track != 'Member Credit' or not self.settlement_reference):
            frappe.throw('Only posted member-wallet credits can become Available')

    def on_trash(self):
        frappe.throw('Referral rewards are audit records and cannot be deleted')
