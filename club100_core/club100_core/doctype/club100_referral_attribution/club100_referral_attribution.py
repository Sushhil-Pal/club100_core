import frappe
from frappe.model.document import Document


class Club100ReferralAttribution(Document):
    def validate(self):
        if self.is_new():
            partner = frappe.get_doc("Club100 Referral Partner", self.partner)
            if partner.status != "Active":
                frappe.throw("Referral partner must be active.")
            if partner.member and partner.member == self.referred_member:
                frappe.throw("Self-referrals are not permitted.")
            existing = frappe.db.exists(self.doctype, {"referred_member": self.referred_member})
            if existing:
                frappe.throw("This member already has a referral attribution.")
        else:
            original = frappe.get_doc(self.doctype, self.name)
            immutable = ("referred_member", "partner", "referral_code", "reward_track", "policy", "first_rate", "renewal_rate", "currency", "minimum_duration_months", "campaign", "captured_on")
            for field in immutable:
                if str(original.get(field) or "") != str(self.get(field) or ""):
                    frappe.throw(f"Referral attribution {field} cannot be changed.")
