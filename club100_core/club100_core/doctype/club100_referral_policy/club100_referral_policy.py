import frappe
from frappe.model.document import Document


class Club100ReferralPolicy(Document):
    def validate(self):
        for name in ("first_purchase_rate", "renewal_rate"):
            rate = frappe.utils.flt(getattr(self, name, 0))
            if rate < 0 or rate > 100:
                frappe.throw(f"{name} must be between 0 and 100.")
        if int(self.minimum_duration_months or 0) < 1:
            frappe.throw("Minimum purchase duration must be at least 1 month.")
        if self.enabled:
            existing = frappe.db.get_value(
                self.doctype,
                {"reward_track": self.reward_track, "enabled": 1, "name": ["!=", self.name or ""]},
                "name",
            )
            if existing:
                frappe.throw("Only one enabled referral policy is supported for each reward track.")
