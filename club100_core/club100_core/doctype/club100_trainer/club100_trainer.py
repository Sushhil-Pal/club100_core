import frappe
from frappe.model.document import Document


class Club100Trainer(Document):
    def validate(self):
        self.validate_delivery_modes()
        self.validate_online_capacity()

    def validate_delivery_modes(self):
        if not self.can_deliver_online and not self.can_deliver_offline:
            frappe.throw(
                "Trainer must be enabled for at least one delivery mode."
            )

    def validate_online_capacity(self):
        if self.can_deliver_online:
            if not self.max_online_cohort_size or self.max_online_cohort_size <= 0:
                frappe.throw(
                    "Max Online Cohort Size must be greater than 0 for online trainers."
                )