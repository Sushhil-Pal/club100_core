from frappe.model.document import Document


class Club100FitnessMetric(Document):
    def validate(self):
        if self.metric_code:
            self.metric_code = (
                self.metric_code
                .strip()
                .upper()
                .replace(" ", "_")
            )