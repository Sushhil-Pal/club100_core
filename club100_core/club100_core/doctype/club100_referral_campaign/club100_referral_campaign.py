import frappe
from frappe.model.document import Document


class Club100ReferralCampaign(Document):
    def validate(self):
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            frappe.throw("Campaign end date cannot precede start date.")
